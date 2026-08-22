#!/usr/bin/env python3
"""
SLAM Pipeline Applet
=====================
A small GUI with one button per pipeline stage:
    1. SLAM          (ouster-cli: pcap/meta -> map .ply)
    2. Level         (corrects SLAM's arbitrary tilt so the floor is horizontal)
    3. Cleanup       (CloudCompare: SOR outlier removal + optional ICP align)
    4. Diff          (CloudCompare: M3C2 change detection between two clouds)
    5. Classify      (thresholds the M3C2 result into real change vs noise)
    6. Surface       (reconstructs a mesh from the classified change cloud)
    7. Export to USD (delegates to an external conversion script)

Each button opens a small dialog for that stage's inputs, then streams the
underlying command's output live into the shared log console at the bottom
so nothing runs silently or "just closes" on you.

PROJECT_SCHEMA_v2.md UPDATE: a project is no longer one linear pipeline.
It now holds a baseline pipeline (Stages 1-3), zero or more comparison
scan pipelines (also Stages 1-3, one full run each), and zero or more
diff pipelines (Stages 4-7, each naming which two cleanup outputs it
compares). This window reflects that with two independent selectors
instead of one "active project":

  - "Source pipeline": the baseline, or one comparison scan - whichever
    one Stages 1-3's buttons act on.
  - "Diff pipeline": one diff entry - whichever one Stages 4-7's buttons
    act on.

Both selectors only exist once a project is open. "New Scan" and
"New Diff" add entries to the open project and can be picked afterward.

Run:
    python pipeline_applet.py
"""

import json
import re
import subprocess
import sys
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path

import pipeline_core as core

try:
    import project_manager as pm
except ImportError:
    pm = None

ABOUT_CONTENT_FILE = Path(__file__).resolve().parent / "about_content.json"


def load_about_content():
    """
    Loads stage descriptions from about_content.json, which sits next to
    this script. Edit that file to update the About panel without touching
    any code. Structure: {"Stage title": {"Field label": "Field text", ...}}
    """
    if not ABOUT_CONTENT_FILE.exists():
        return {"About": {"Notice": f"about_content.json not found next to "
                                     f"{Path(__file__).name} - nothing to show."}}
    try:
        with open(ABOUT_CONTENT_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        return {"About": {"Error": f"Could not read about_content.json: {e}"}}


def _pipeline_label_for(kind, pipeline_id):
    """Short display label from a PipelineHandle's raw kind/pipeline_id
    (rather than the handle itself) - e.g. 'Baseline',
    'Scan: post-storm_2026-09-01', 'Diff: post-storm_2026-09-01_vs_baseline'.
    Used directly by ProjectFilePicker, which only has the raw kind/id
    pairs project_manager.list_eligible_inputs()/list_side_candidates()
    return (PROJECT_INPUT_PICKER_PLAN.md Section 4.1 - that module stays
    GUI-free by design, so it never builds display strings itself)."""
    if kind == "baseline":
        return "Baseline"
    if kind == "scan":
        return f"Scan: {pipeline_id}"
    return f"Diff: {pipeline_id}"


def _pipeline_label(pipeline):
    """Short display label for a PipelineHandle, e.g. 'Baseline',
    'Scan: post-storm_2026-09-01', 'Diff: post-storm_2026-09-01_vs_baseline'."""
    if pipeline is None:
        return "(none selected)"
    return _pipeline_label_for(pipeline.kind, pipeline.pipeline_id)


# Display names for a project_manager group dict's "stage_name" - used by
# ProjectFilePicker to label each stage's sub-group in its tree
# (PROJECT_INPUT_PICKER_PLAN.md Section 5.1). "raw" is the sentinel
# list_eligible_inputs() uses for a pipeline's raw import group (Stage 1
# has no earlier STAGE to group by).
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
# input field (PROJECT_INPUT_PICKER_PLAN.md Section 5.1). Replaces the old
# auto-resolve + "manual override" checkbox: a stage's input field now
# always starts blank, and this is the second of the two ways to fill it
# (typing/Browse... being the first, unchanged from manual mode).
# ---------------------------------------------------------------------------

class ProjectFilePicker(tk.Toplevel):
    """A popup listing every file a project-mode input field could use,
    grouped by pipeline then by stage. Real, non-clickable group headers
    via ttk.Treeview - a plain ttk.Combobox has no way to make a header
    row unselectable, which is why this is a separate popup rather than
    a fancier combobox.

    groups: the group-dict list returned by
    project_manager.list_eligible_inputs() / list_side_candidates() -
    each dict's "path" values are project-relative; this resolves them
    to absolute paths (via project_manager.get_absolute_path()) before
    calling on_pick(), so on_pick always receives something a stage
    dialog's field can use directly, same as Browse... would put there.

    Only a leaf (file) row is selectable; picking one (double-click, or
    Choose) calls on_pick(absolute_path) and closes the popup. Selecting
    a pipeline or stage header and pressing Choose does nothing - there
    is nothing to pick there.
    """

    def __init__(self, parent, project, groups, on_pick):
        super().__init__(parent)
        self.title("Choose a Project File")
        self.resizable(True, True)
        self.geometry("640x420")
        self.minsize(480, 320)  # keeps the Choose/Cancel buttons reachable
        self._project = project
        self._on_pick = on_pick
        self._path_by_item = {}

        if not groups:
            ttk.Label(self, text="No eligible files yet - run an earlier stage first.",
                      padding=20, wraplength=400, justify="left").pack()
            ttk.Button(self, text="Close", command=self.destroy).pack(pady=(0, 12))
            return

        tree_frame = ttk.Frame(self, padding=8)
        tree_frame.pack(fill="both", expand=True)
        tree = ttk.Treeview(tree_frame, show="tree", selectmode="browse")
        tree.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(tree_frame, command=tree.yview)
        scrollbar.pack(side="right", fill="y")
        tree.config(yscrollcommand=scrollbar.set)

        pipeline_nodes = {}
        for group in groups:
            pipeline_key = (group["pipeline_kind"], group["pipeline_id"])
            pipeline_node = pipeline_nodes.get(pipeline_key)
            if pipeline_node is None:
                pipeline_node = tree.insert(
                    "", "end", text=_pipeline_label_for(*pipeline_key), open=True)
                pipeline_nodes[pipeline_key] = pipeline_node

            stage_label = STAGE_DISPLAY_NAMES.get(group["stage_name"],
                                                   group["stage_name"].title())
            stage_node = tree.insert(pipeline_node, "end", text=stage_label, open=True)

            for f in group["files"]:
                item = tree.insert(stage_node, "end", text=self._file_display(f))
                self._path_by_item[item] = f["path"]

        def pick_selected():
            selection = tree.selection()
            if not selection:
                return
            rel_path = self._path_by_item.get(selection[0])
            if rel_path is None:
                return  # a pipeline/stage header, not a file - nothing to pick
            self._on_pick(pm.get_absolute_path(self._project, rel_path))
            self.destroy()

        tree.bind("<Double-1>", lambda event: pick_selected())

        button_frame = ttk.Frame(self, padding=(8, 0, 8, 8))
        button_frame.pack(fill="x")
        ttk.Button(button_frame, text="Choose", command=pick_selected).pack(side="right")
        ttk.Button(button_frame, text="Cancel", command=self.destroy
                   ).pack(side="right", padx=(0, 6))

    @staticmethod
    def _file_display(f):
        name = Path(f["path"]).name
        if f.get("note"):
            return f"{name} ({f['note']})"
        tag = "current" if f.get("is_current") else f"pass {f['sequence']}"
        return f"{name} ({tag})"


# ---------------------------------------------------------------------------
# Small reusable dialog helpers
# ---------------------------------------------------------------------------

class StageDialog(tk.Toplevel):
    """Base class for a stage's input dialog. Subclasses build their own
    rows of fields and implement build_command()."""

    def __init__(self, parent, title, run_callback):
        super().__init__(parent)
        self.title(title)
        self.resizable(True, True)
        self.minsize(300, 200)  # a small placeholder only, in effect for the brief
                                 # moment before add_run_button() sets the REAL
                                 # floor from this dialog's own measured content
                                 # (see there) - never actually visible to the
                                 # user under normal use
        self.run_callback = run_callback  # called with the built command list
        self.fields = {}
        self.field_widgets = {}  # key -> list of widgets (Entry, Browse button(s)) -
                                  # tracked for require_existing_file()'s error messages
                                  # and any future need to look a field's widgets back up
        self.row = 0

        # Root layout: a scrollable canvas holding every field row (self.form,
        # unchanged as the target every add_*_field()/add_hint()/etc. method
        # grids into), with a separate, always-visible footer below it for
        # Run/Cancel (see add_run_button). This guarantees Run stays reachable
        # regardless of how many fields/hints a given dialog has or how short
        # the window gets shrunk - only the field area scrolls, never the
        # footer - rather than trying to guess a tall-enough fixed minsize for
        # every dialog's own, very different, natural content height.
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)

        canvas_frame = ttk.Frame(self)
        canvas_frame.grid(row=0, column=0, sticky="nsew")
        canvas_frame.grid_rowconfigure(0, weight=1)
        canvas_frame.grid_columnconfigure(0, weight=1)

        canvas = tk.Canvas(canvas_frame, highlightthickness=0)
        canvas.grid(row=0, column=0, sticky="nsew")
        self._canvas = canvas  # sized once, in add_run_button(), after every
                                # field for this specific dialog has been added
                                # - see add_run_button()'s docstring note
        scrollbar = ttk.Scrollbar(canvas_frame, orient="vertical", command=canvas.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        canvas.configure(yscrollcommand=scrollbar.set)

        self.form = ttk.Frame(canvas, padding=12)
        form_window = canvas.create_window((0, 0), window=self.form, anchor="nw")

        def _on_form_configure(event=None):
            canvas.configure(scrollregion=canvas.bbox("all"))
        self.form.bind("<Configure>", _on_form_configure)

        def _on_canvas_configure(event):
            # Keeps the inner form frame's width matched to the canvas's own
            # width, so wrapped text (add_hint, the SLAM backend radio labels)
            # and columnspan=3 rows reflow with the window instead of the
            # form staying stuck at whatever width it first opened at.
            canvas.itemconfigure(form_window, width=event.width)
        canvas.bind("<Configure>", _on_canvas_configure)

        # Mousewheel scrolling, Windows-only syntax (event.delta in multiples
        # of 120) - fine given this project's environment is Windows
        # throughout. Bound only while the mouse is actually over this
        # canvas (bind on Enter, unbind on Leave) rather than globally for
        # the whole app - binding globally would make whichever dialog last
        # grabbed the wheel event scroll instead of whichever one the mouse
        # is actually over, if more than one happens to be open at once.
        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        def _bind_mousewheel(event):
            canvas.bind_all("<MouseWheel>", _on_mousewheel)

        def _unbind_mousewheel(event):
            canvas.unbind_all("<MouseWheel>")

        canvas.bind("<Enter>", _bind_mousewheel)
        canvas.bind("<Leave>", _unbind_mousewheel)

        self.footer = ttk.Frame(self, padding=(12, 8, 12, 12))
        self.footer.grid(row=1, column=0, sticky="ew")

        self._section_stack = []

    def begin_section(self):
        """Starts a group of fields that can be shown/hidden together as
        one unit later - e.g. open_slam_dialog() uses this so only the
        Backend currently selected via its radio buttons has its fields
        visible, instead of always showing both backends' fields at once
        regardless of which one is in use.

        Works by creating a child Frame that occupies exactly one row of
        the CURRENT form, then temporarily redirecting self.form/self.row
        to that child frame - every add_* method below (add_text_field,
        add_file_field, add_checkbox, add_preset_selector,
        add_radio_choice, add_hint, ...) already just reads/writes
        self.form and self.row, so nothing about them needs to change:
        they build inside the child frame, using ITS own independent
        row numbering, for as long as a section is open. Call
        end_section() to close it and resume adding fields to whatever
        was the form before (the main dialog form, or an outer section,
        so sections can nest if ever needed).

        Returns the child Frame - hide/show the whole group later with
        section.grid_remove() / section.grid()."""
        section = ttk.Frame(self.form)
        section.grid(row=self.row, column=0, columnspan=3, sticky="ew")
        self.row += 1  # the section occupies exactly one row of the OUTER form
        self._section_stack.append((self.form, self.row))
        self.form = section
        self.row = 0
        return section

    def end_section(self):
        """Closes a group started with begin_section(), resuming field
        placement in whatever was the form before (see begin_section())."""
        self.form, self.row = self._section_stack.pop()

    def add_file_field(self, key, label, filetypes=(("All files", "*.*"),), default=""):
        ttk.Label(self.form, text=label).grid(row=self.row, column=0, sticky="w", pady=3)
        var = tk.StringVar(value=default)
        entry = ttk.Entry(self.form, textvariable=var, width=55)
        entry.grid(row=self.row, column=1, padx=5)

        def browse():
            path = filedialog.askopenfilename(parent=self, filetypes=filetypes)
            if path:
                var.set(path)

        browse_btn = ttk.Button(self.form, text="Browse...", command=browse)
        browse_btn.grid(row=self.row, column=2)
        self.fields[key] = var
        self.field_widgets[key] = [entry, browse_btn]
        self.row += 1

    def add_file_or_folder_field(self, key, label, filetypes=(("All files", "*.*"),), default="",
                                  on_picked=None):
        """Like add_file_field, but with a second button for picking a
        folder instead - needed for sources that can be either a single
        file (pcap, .bag) or a directory (a ROS2 bag, which is a folder
        containing .db3 + metadata.yaml).

        on_picked: optional callback, called with the picked path (a str)
        right after either button sets it - e.g. open_slam_dialog() uses
        this to check a picked ROS2 bag folder for raw Ouster packets and
        offer to convert them (see _check_and_convert_raw_bag() there).
        Not called for a path typed or pasted directly into the entry -
        a dialog that wants that too should also expose an explicit
        "check" button, since silently reacting to every keystroke would
        be noisy."""
        ttk.Label(self.form, text=label).grid(row=self.row, column=0, sticky="w", pady=3)
        var = tk.StringVar(value=default)
        entry = ttk.Entry(self.form, textvariable=var, width=55)
        entry.grid(row=self.row, column=1, padx=5)

        def browse_file():
            path = filedialog.askopenfilename(parent=self, filetypes=filetypes)
            if path:
                var.set(path)
                if on_picked:
                    on_picked(path)

        def browse_folder():
            path = filedialog.askdirectory(parent=self)
            if path:
                var.set(path)
                if on_picked:
                    on_picked(path)

        button_frame = ttk.Frame(self.form)
        button_frame.grid(row=self.row, column=2)
        file_btn = ttk.Button(button_frame, text="File...", command=browse_file)
        file_btn.pack(side="left")
        folder_btn = ttk.Button(button_frame, text="Folder...", command=browse_folder)
        folder_btn.pack(side="left")
        self.fields[key] = var
        self.field_widgets[key] = [entry, file_btn, folder_btn]
        self.row += 1

    def add_save_field(self, key, label, default_ext=".ply", default=""):
        ttk.Label(self.form, text=label).grid(row=self.row, column=0, sticky="w", pady=3)
        var = tk.StringVar(value=default)
        entry = ttk.Entry(self.form, textvariable=var, width=55)
        entry.grid(row=self.row, column=1, padx=5)

        def browse():
            path = filedialog.asksaveasfilename(parent=self, defaultextension=default_ext)
            if path:
                var.set(path)

        ttk.Button(self.form, text="Browse...", command=browse).grid(row=self.row, column=2)
        self.fields[key] = var
        self.row += 1

    def add_text_field(self, key, label, default=""):
        ttk.Label(self.form, text=label).grid(row=self.row, column=0, sticky="w", pady=3)
        var = tk.StringVar(value=default)
        ttk.Entry(self.form, textvariable=var, width=55).grid(row=self.row, column=1, padx=5, columnspan=2, sticky="w")
        self.fields[key] = var
        self.row += 1

    def add_checkbox(self, key, label, default=False):
        # NOTE: like add_radio_choice above, ttk.Checkbutton rejects "-wraplength"
        # outright on this Tcl/Tk build - if a future label needs wrapping, add
        # embedded "\n" line breaks in the label text at the call site instead.
        var = tk.BooleanVar(value=default)
        ttk.Checkbutton(self.form, text=label, variable=var).grid(
            row=self.row, column=0, columnspan=3, sticky="w", pady=3)
        self.fields[key] = var
        self.row += 1

    def add_hint(self, text):
        """Small muted, wrapped label for inline guidance under a field -
        typical ranges, what a value does, when to leave something blank."""
        ttk.Label(self.form, text=text, foreground="#777",
                  font=("Segoe UI", 8), wraplength=380, justify="left"
                  ).grid(row=self.row, column=0, columnspan=3, sticky="w", pady=(0, 8))
        self.row += 1

    def add_preset_selector(self, label, presets, hint=None):
        """
        presets: list of (display_label, {field_key: value, ...}) tuples.
        Selecting an option fills the given field(s) - which can already
        exist or be created later in this same build function, since the
        fill only happens when the user picks an option, by which point
        all fields exist. The underlying field(s) stay editable afterward,
        so a preset is a starting point, not a lock.
        """
        ttk.Label(self.form, text=label).grid(row=self.row, column=0, sticky="w", pady=3)
        combo_var = tk.StringVar()
        combo = ttk.Combobox(self.form, textvariable=combo_var, state="readonly",
                              width=75, values=[p[0] for p in presets])
        combo.grid(row=self.row, column=1, columnspan=2, padx=5, sticky="w")
        preset_map = {p[0]: p[1] for p in presets}

        def on_select(event=None):
            values = preset_map.get(combo_var.get())
            if values:
                for field_key, field_value in values.items():
                    self.fields[field_key].set(field_value)

        combo.bind("<<ComboboxSelected>>", on_select)
        self.row += 1
        if hint:
            self.add_hint(hint)
        return combo

    def add_radio_choice(self, key, label, options, default=None, hint=None):
        """Adds a row of radio buttons for a genuinely discrete choice - a
        small, fixed set of valid values where anything outside that set
        is meaningless (e.g. Backend: 'ouster' or 'kiss_icp', Method:
        'poisson' or 'ball_pivoting'). This is NOT the right control for a
        continuous value that just has a few common starting points (e.g.
        voxel size, distance threshold) - those stay better served by
        add_preset_selector() paired with a free-editable field, since a
        user legitimately might want a value between or beyond the
        presets.

        Replaces what Backend and Method used to be: a preset Combobox
        PLUS a separate free-text Entry sharing the same field - two
        controls for one value, where the free-text one could be left
        blank or typo'd into something that silently wasn't any valid
        choice. That's exactly what happened with Stage 1's Backend field
        before it was fixed elsewhere in this file (a blank Backend used
        to silently break the raw-packet check with no feedback at all) -
        a radio button makes an invalid/blank value structurally
        impossible instead of something to keep guarding against.

        options: list of (display_label, value) tuples. `default` picks
        which option starts selected by its VALUE (not its display
        label) - defaults to the first option if not given."""
        ttk.Label(self.form, text=label).grid(row=self.row, column=0, sticky="nw", pady=3)
        start_value = default if default is not None else options[0][1]
        var = tk.StringVar(value=start_value)
        radio_frame = ttk.Frame(self.form)
        radio_frame.grid(row=self.row, column=1, columnspan=2, sticky="w", padx=5)
        for display_label, value in options:
            ttk.Radiobutton(radio_frame, text=display_label, variable=var, value=value
                             # NOTE: ttk.Radiobutton on this Tcl/Tk build rejects
                             # "-wraplength" as an unknown option outright (confirmed via a
                             # real TclError, not a guess) - unlike tk.Label, which accepts
                             # it fine (see add_hint below). So a long option's display_label
                             # needs its own embedded "\n" line breaks at the call site
                             # instead of relying on any automatic wrapping here.
                             ).pack(anchor="w", fill="x")
        self.fields[key] = var
        self.row += 1
        if hint:
            self.add_hint(hint)

    def add_pipeline_label(self, pipeline):
        """Small context line naming which project pipeline this dialog
        is acting on - e.g. 'Pipeline: roomA / Baseline'. Purely
        informational: no input resolution and no checkbox (removed
        along with the old add_project_header() -
        PROJECT_INPUT_PICKER_PLAN.md Section 5.3 - project-mode input
        fields now start blank like any other field; see
        add_project_input_field())."""
        compartment = pipeline.project.data.get("compartment", "?")
        ttk.Label(self.form, text=f"Pipeline: {compartment} / {_pipeline_label(pipeline)}",
                  foreground="#0a5", font=("Segoe UI", 9, "bold")
                  ).grid(row=self.row, column=0, columnspan=3, sticky="w", pady=(0, 6))
        self.row += 1

    def add_project_picker_button(self, field_key, project, groups_fn, extra_on_pick=None):
        """Adds a "Choose from project..." button on field_key's own row
        (column 3) - the field itself must already have been added THIS
        CALL, via add_file_field() / add_file_or_folder_field()
        (immediately before this call, so self.row - 1 is still that
        field's row). Opens a ProjectFilePicker built from groups_fn() (a
        zero-arg callable returning a project_manager group-dict list -
        pm.list_eligible_inputs()/list_side_candidates(), wrapped in a
        lambda so it re-reads the project fresh each time the button is
        pressed) and fills field_key with whatever gets picked there.

        This is a second way to fill the field, not a separate mode -
        typing or Browse... still work exactly the same
        (PROJECT_INPUT_PICKER_PLAN.md Section 3). Every project-mode
        input field starts blank; nothing here pre-fills anything.

        extra_on_pick: optional callable(absolute_path), run right
        after the field is set - e.g. open_slam_dialog() also runs its
        raw-packet check on whatever gets picked this way, matching
        what already happens when Browse-ing to the same kind of path.
        """
        def on_pick(path):
            self.fields[field_key].set(path)
            if extra_on_pick:
                extra_on_pick(path)

        row = self.row - 1
        button = ttk.Button(
            self.form, text="Choose from project...",
            command=lambda: ProjectFilePicker(self, project, groups_fn(), on_pick=on_pick))
        button.grid(row=row, column=3, padx=(6, 0))

    def add_registered_baseline_preset(self, field_key,
                                        label="Or pick a manual-mode registered baseline:"):
        """Adds a preset dropdown of every manual-mode registered baseline
        (baseline_registry.json, via core.list_compartments()/
        get_active_baseline()) that fills `field_key` when picked. A no-op
        (adds nothing) if no baselines are registered. Used by Stage 3
        (Cleanup)'s 'align_to' field and Stage 5 (Diff)'s 'baseline' field
        - previously two copies of the same six lines, differing only in
        which field key they filled."""
        known_compartments = core.list_compartments()
        if not known_compartments:
            return
        self.add_preset_selector(label, [
            (f"{name} -> {Path(core.get_active_baseline(name)).name}",
             {field_key: core.get_active_baseline(name)})
            for name in known_compartments
        ])

    @staticmethod
    def resolve_project_output_default(pipeline, stage_name, extension):
        """Returns this stage's default project-mode Output path (an
        absolute path string), or "" if no pipeline is set or the path
        can't be resolved yet (e.g. project.json doesn't have enough
        recorded history for get_output_path to compute one). Every
        Stage 1-8 dialog's Output field default used to repeat this exact
        try/except ProjectError block inline - factored out here since it
        was identical in all six places."""
        if pipeline is None:
            return ""
        try:
            return pm.get_absolute_path(pipeline.project, pm.get_output_path(pipeline, stage_name, extension))
        except pm.ProjectError:
            return ""

    def get_active_pipeline_for_run(self):
        """Returns the pipeline to actually use for this run: whatever
        was set on self.pipeline (each open_*_dialog() sets this
        directly, from the main window's Source/Diff pipeline selector),
        or None in manual mode (no pipeline selected at all).

        There is no more "auto vs. manual override" distinction
        (PROJECT_INPUT_PICKER_PLAN.md Section 5.4) - picking a file via
        ProjectFilePicker is just the normal way to fill a project-mode
        field now, not a special case, so every run with a pipeline set
        gets recorded into project.json, regardless of which file was
        picked or how."""
        return getattr(self, "pipeline", None)

    def add_run_button(self, build_command_fn):
        """build_command_fn must return (cmd, report), or optionally
        (cmd, report, finish_info) for a project-mode-aware dialog.
        report is either a string, or a zero-arg callable (deferred -
        called after the run finishes, so it can inspect files the run
        just created, e.g. to rename/discover the actual output). Shown
        in a popup on success.

        finish_info, if given, is a dict:
            {"pipeline": ..., "stage_name": ..., "output": ...,
             "resolve_state": {...} (optional)}
        "pipeline" may be None (manual mode), in which case nothing
        project-related happens. When "pipeline" is a real
        PipelineHandle, once the run finishes AND the report has been
        built, core.finish_stage() is called automatically to report
        success/failure back to it. "output"/"extra_fields"/"log_path"
        are taken from "resolve_state" (a dict the report callable can
        populate as a side effect, for stages like Cleanup/Diff where
        CloudCompare's real output filename is only known after the run)
        when present, falling back to the static "output" otherwise."""
        self.status_label = ttk.Label(self.footer, text="", foreground="#555")
        self.status_label.pack(pady=(6, 0))

        def on_run():
            try:
                result = build_command_fn()
            except ValueError as e:
                messagebox.showerror("Missing input", str(e))
                return

            if len(result) == 3:
                cmd, report_text, finish_info = result
            else:
                cmd, report_text = result
                finish_info = None

            self._pending_report = report_text
            self._pending_finish_info = finish_info
            self._set_busy(True)
            self.status_label.config(text="Running - see main window log for progress...")
            self.run_callback(cmd, on_complete=self._on_stage_complete)

        self.run_button = ttk.Button(self.footer, text="Run", command=on_run)
        self.run_button.pack()

        # add_run_button() is always the LAST call in every open_*_dialog()
        # function (every field/hint has already been added by now), so this
        # is the right moment to measure this dialog's own real natural
        # content size and open the window at exactly that - rather than
        # relying on a single shared minsize guess to cover every dialog's
        # very different content. Width is left uncapped: there's no
        # horizontal scrollbar, so anything wider than the window would be
        # unreachable, not just less convenient. Height IS capped, since the
        # canvas above is scrollable - a very tall dialog opens at a
        # reasonable size and scrolls for the rest, rather than trying to
        # fit everything on screen at once regardless of monitor size.
        self.update_idletasks()
        content_width = self.form.winfo_reqwidth()
        content_height = self.form.winfo_reqheight()
        self._canvas.configure(
            width=content_width,
            height=min(content_height, 650),
        )
        self.update_idletasks()
        self.geometry("")  # forces Tkinter to redo the Toplevel's own outer
                            # sizing pass using the canvas's just-updated
                            # width/height - the update_idletasks() call
                            # above already did one layout pass using the
                            # canvas's OLD (small, default) size, before it
                            # had been told its real size; this makes it
                            # recompute using the corrected size instead of
                            # possibly staying stuck at that first pass

        # Sets the REAL floor now that this dialog's actual content is known,
        # replacing __init__'s placeholder minsize. Width matches the
        # measured content exactly (plus a little slack for the scrollbar) -
        # unlike height, there's no horizontal scrollbar to fall back on, so
        # anything narrower than this would just be unreachable, not merely
        # in need of a scroll. Height's floor is a fixed, comfortable
        # minimum (or this dialog's own natural height, if that's already
        # smaller) rather than whatever the smallest technically-still-
        # scrollable height would be - a floor so short it shows barely any
        # fields above Run doesn't feel like a usable minimum even if
        # nothing is technically unreachable.
        self.minsize(content_width + 24, min(content_height, 450))

    def _set_busy(self, busy):
        state = "disabled" if busy else "normal"
        for child in list(self.form.winfo_children()) + list(self.footer.winfo_children()):
            try:
                child.configure(state=state)
            except tk.TclError:
                pass  # some widgets (e.g. labels) don't support 'state'
        self.protocol("WM_DELETE_WINDOW", (lambda: None) if busy else self.destroy)

    def _on_stage_complete(self, returncode):
        # Called from the main window once the background process exits.
        success = (returncode == 0)
        finish_info = getattr(self, "_pending_finish_info", None)
        pipeline = finish_info.get("pipeline") if finish_info else None

        # Build the report FIRST (it may resolve the real output path/RMS/
        # log path for stages like Cleanup/Diff, via resolve_state below),
        # then report to the project - so a project-mode stage's recorded
        # output/extra_fields reflect what actually happened, not just the
        # name that was asked for.
        report = None
        if success:
            report = getattr(self, "_pending_report", "")
            if callable(report):
                try:
                    report = report()
                except Exception as e:
                    report = f"(Could not build the full report: {e})"

        if pipeline is not None:
            output = finish_info.get("output")
            extra_fields = None
            log_path = None
            resolve_state = finish_info.get("resolve_state")
            if resolve_state:
                output = resolve_state.get("output") or output
                extra_fields = resolve_state.get("extra_fields")
                log_path = resolve_state.get("log_path")
            core.finish_stage(
                pipeline, finish_info["stage_name"], output,
                success=success,
                error_message=None if success else f"Exited with code {returncode}",
                extra_fields=extra_fields,
                log_path=log_path,
            )
            if hasattr(self.master, "refresh_project_status"):
                self.master.refresh_project_status()

        if success:
            if pipeline is not None:
                try:
                    output_for_display = (finish_info.get("resolve_state") or {}).get("output") \
                        or finish_info.get("output")
                    rel = str(Path(output_for_display).relative_to(pipeline.project.root))
                except (ValueError, TypeError):
                    rel = str(finish_info.get("output"))
                report = (report or "") + f"\n\nSaved to project: {rel}"
            self._set_busy(False)  # dialog stays open - re-enables fields/Run
                                    # (e.g. to run again with different
                                    # params) instead of closing automatically
            self.status_label.config(text="Done.", foreground="#0a5")
            if report:
                messagebox.showinfo("Stage Report", report)
        else:
            self._set_busy(False)
            self.status_label.config(
                text=f"Failed (exit code {returncode}) - see main window log for details.",
                foreground="#b00")

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
                "If you picked this from a preset dropdown, that entry may be "
                "stale (the file was moved/renamed/deleted since it was "
                "registered). Either fix the path or browse to the right file."
            )
        return val


# ---------------------------------------------------------------------------
# Stage-specific dialogs: Stage 1-4 (baseline / scan pipelines)
# ---------------------------------------------------------------------------

def open_slam_dialog(parent, run_callback, pipeline=None):
    dlg = StageDialog(parent, "Stage 1: SLAM", run_callback)
    pipeline = pipeline if (pipeline is not None and pipeline.kind in ("baseline", "scan")) else None
    dlg.pipeline = pipeline

    if pipeline is not None:
        dlg.add_pipeline_label(pipeline)

    dlg.add_radio_choice("backend", "Backend:", [
        ("Ouster CLI - reads raw sensor packets directly (pcap/OSF/rosbag)\n"
         "- no decoding needed", "ouster"),
        ("KISS-ICP - different algorithm entry point, reads already-decoded\n"
         "PointCloud2 rosbag topics only - a source with only raw packets must\n"
         "be decoded first (see 'Check Source for Raw Packets...' below)", "kiss_icp"),
    ], hint=(
        "Both backends can point at the same source file for side-by-side "
        "comparison - just switch Backend and the output filename."))

    def _check_and_convert_raw_bag(path, interactive=False):
        """Called right after a source is picked (interactive=False - stays
        quiet when there's nothing to do, so browsing isn't noisy), and by
        the explicit 'Check Source for Raw Packets...' button
        (interactive=True - always shows something, even 'nothing to do',
        so the button never looks like it did nothing).

        Offers to convert raw packets to decoded points regardless of which
        Backend is selected - Ouster CLI can already read raw packets
        directly, so it doesn't NEED this, but a decoded bag is still
        useful to have (comparing backends side by side, a future SLAM
        method, eventual use outside this pipeline via a plain `ros2 bag
        play`) - so this asks either way and lets the user decide, rather
        than deciding for them based on Backend.

        Previously this skipped entirely whenever Backend was 'ouster' -
        and an empty Backend field silently fell back to acting as if
        'ouster' were selected ('backend = ... or "ouster"'), so this
        returned with no feedback at all whenever Backend hadn't been
        explicitly set either - indistinguishable from the button doing
        nothing. Both are fixed now: Backend is no longer read at all here,
        and this always offers to convert whenever raw packets are found,
        regardless of what Backend is set to (or left blank as)."""
        try:
            info = core.inspect_rosbag_topics(path)
        except RuntimeError as e:
            messagebox.showwarning("Could not check for raw packets", str(e))
            return
        if info is None:
            if interactive:
                messagebox.showinfo("Not a ROS2 bag folder",
                                     f"'{Path(path).name}' doesn't look like a ROS2 bag folder "
                                     f"(no metadata.yaml found) - nothing to check.")
            return
        if info["has_pointcloud2"] or not info["raw_lidar_topic"]:
            if interactive:
                messagebox.showinfo("No raw packets found",
                                     f"'{Path(path).name}' already has a decoded points topic, "
                                     f"or no raw lidar packet topic was found - nothing to "
                                     f"convert.")
            return

        if not info["metadata_topic"]:
            messagebox.showwarning(
                "Raw packets found, but no metadata topic",
                f"This bag has raw packets on '{info['raw_lidar_topic']}' "
                f"({info['raw_lidar_count']} packets), but no metadata topic (a "
                f"std_msgs/String topic with 'metadata' in its name) to decode them "
                f"with. Convert by hand with decode_raw_packets.py, passing "
                f"--metadata-topic explicitly, or point KISS-ICP at a bag that "
                f"already has decoded points."
            )
            return

        proceed = messagebox.askyesno(
            "Raw packets detected",
            f"'{Path(path).name}' has raw Ouster packets on '{info['raw_lidar_topic']}' "
            f"({info['raw_lidar_count']} packets), not decoded points. If Backend is "
            f"KISS-ICP, its rosbag dataloader needs decoded points and will fail on this "
            f"bag as-is. If Backend is Ouster CLI, it can already read these raw packets "
            f"directly - converting is optional there, but still useful to have a decoded "
            f"copy on hand.\n\n"
            f"Convert to a new decoded bag now, using decode_raw_packets.py?"
        )
        if not proceed:
            return

        def _apply_decoded_source():
            """Makes `output_bag` actually the source used for this SLAM
            stage, and reports what that means in plain terms.

            The dialog's own "source" field is always what a Run
            actually uses (PROJECT_INPUT_PICKER_PLAN.md Section 5.5 -
            build_slam_command()/build_kiss_icp_slam_command() no longer
            re-resolve the input from a pipeline behind the field's
            back), so filling it in here is enough on its own. If a
            project pipeline is active, also record the decoded bag on
            the PIPELINE itself via project_manager.set_decoded_raw_path()
            - purely so it shows up as a 'decoded' option in this
            pipeline's Raw import group in ProjectFilePicker next time
            too, not because anything here depends on that record.

            Returns the sentence to show in the popup."""
            pipeline = getattr(dlg, "pipeline", None)
            dlg.fields["source"].set(str(output_bag))
            if pipeline is not None:
                pm.set_decoded_raw_path(pipeline, output_bag)
                return ("Also recorded on this project pipeline, so it shows up as a "
                         "'decoded' option under 'Choose from project...' here next time too.")
            return "Source has been updated to use it."

        output_bag = Path(path).parent / f"{Path(path).name}_decoded"
        if output_bag.exists():
            explanation = _apply_decoded_source()
            messagebox.showinfo(
                "Already converted",
                f"A decoded bag already exists at:\n{output_bag}\n\n{explanation}")
            return

        script = Path(__file__).resolve().parent / "decode_raw_packets.py"
        if not script.exists():
            messagebox.showerror("Missing script",
                                  f"decode_raw_packets.py not found next to the applet:\n{script}")
            return

        cmd = core.build_decode_command(
            script, path, output_bag, lidar_topic=info["raw_lidar_topic"],
            imu_topic=info["raw_imu_topic"], metadata_topic=info["metadata_topic"])

        def on_decode_complete(returncode):
            if returncode == 0:
                explanation = _apply_decoded_source()
                messagebox.showinfo("Converted",
                                     f"Decoded bag saved to:\n{output_bag}\n\n{explanation}")
            else:
                messagebox.showerror(
                    "Conversion failed",
                    f"decode_raw_packets.py exited with code {returncode} - see the "
                    f"main window log for details.")

        run_callback(cmd, on_complete=on_decode_complete)

    dlg.add_file_or_folder_field(
        "source", "Source (pcap / OSF / .bag / ROS2 bag folder):",
        [("PCAP files", "*.pcap"), ("OSF files", "*.osf"), ("ROS bag files", "*.bag"),
         ("All files", "*.*")],
        on_picked=_check_and_convert_raw_bag)
    if pipeline is not None:
        dlg.add_project_picker_button(
            "source", pipeline.project, lambda: pm.list_eligible_inputs(pipeline, "slam"),
            extra_on_pick=_check_and_convert_raw_bag)
    ttk.Button(dlg.form, text="Check Source for Raw Packets...",
               command=lambda: _check_and_convert_raw_bag(
                   dlg.fields["source"].get().strip(), interactive=True)
               ).grid(row=dlg.row, column=0, columnspan=3, sticky="w", pady=(0, 4))
    dlg.row += 1
    dlg.add_hint("Use 'File...' for a .pcap/.osf/ROS1 .bag, 'Folder...' for a ROS2 bag "
                 "(a folder with .db3 + metadata.yaml). A ROS2 bag folder is auto-checked "
                 "for raw packets and offered for conversion regardless of Backend. Needs "
                 "the 'rosbags' package (comes with ouster-sdk; 'pip install rosbags' if "
                 "missing).")
    dlg.add_file_field("meta", "Meta JSON file (optional for rosbag sources):",
                        [("JSON files", "*.json"), ("All files", "*.*")])
    dlg.add_hint("Ouster CLI: required for .pcap, optional for rosbag (resolved from the "
                 "bag if left blank). KISS-ICP: only used with the 'ouster' dataloader - "
                 "exact effect not yet confirmed.")

    output_default = dlg.resolve_project_output_default(pipeline, "slam", ".ply")
    dlg.add_save_field("output", "Output .ply:", default_ext=".ply", default=output_default)

    # Only the fields for the currently-selected Backend are shown - see
    # begin_section()/end_section() and the trace wired up below. Previously
    # both backends' fields were always visible together, labeled "[Ouster
    # CLI]"/"[KISS-ICP]" to hint which applied - correct, but wasted space
    # and made the dialog look more complex than the choice actually in
    # front of the user at any one time.
    ouster_section = dlg.begin_section()
    dlg.add_preset_selector("Voxel size preset:", [
        ("Fine (0.15m) - slower, most detail", {"voxel_size": "0.15"}),
        ("Medium (0.25m) - balanced, good default", {"voxel_size": "0.25"}),
        ("Coarse (0.5m) - fastest, least detail", {"voxel_size": "0.5"}),
    ])
    dlg.add_text_field("voxel_size", "Voxel size (m):", default="0.25")
    dlg.add_hint("Smaller = more detail but slower, larger files. Start with Medium unless "
                 "you have a specific reason to change it.")
    dlg.add_checkbox("visualize", "Open visualizer after processing")
    dlg.end_section()

    kiss_section = dlg.begin_section()
    dlg.add_file_field("kiss_icp_script", "Script (.py):", [("Python files", "*.py")],
                        default=str(Path(__file__).resolve().parent / "slam_kiss_icp.py"))
    _bundled_kiss_icp_config = Path(__file__).resolve().parent / "kiss_icp_config_indoor.yaml"
    dlg.add_file_field(
        "kiss_icp_config", "Config .yaml (recommended):",
        [("YAML files", "*.yaml;*.yml"), ("All files", "*.*")],
        default=str(_bundled_kiss_icp_config) if _bundled_kiss_icp_config.exists() else "")
    dlg.add_hint("Strongly recommended for indoor use - without it, kiss-icp's defaults "
                 "assume vehicle-scale outdoor odometry (max_range=100m), confirmed to "
                 "under-populate an indoor map ~190x (2,628 vs 503,189 points on the same "
                 "real capture). Defaults to the bundled kiss_icp_config_indoor.yaml if "
                 "present - double check it hasn't been swapped for a bag's own "
                 "metadata.yaml (same common filename, different file).")

    config_voxel_size_label = ttk.Label(kiss_section, text="", foreground="#0a5",
                                         font=("Segoe UI", 8))
    config_voxel_size_label.grid(row=dlg.row, column=1, columnspan=2, sticky="w", padx=5)
    dlg.row += 1

    def _refresh_config_voxel_size_label(*_args):
        value = core.read_kiss_icp_voxel_size(dlg.fields["kiss_icp_config"].get().strip())
        config_voxel_size_label.config(
            text=(f"This config's own voxel_size: {value} m"
                  if value is not None else
                  "Could not read a voxel_size from this config (missing, unreadable, or "
                  "left at kiss-icp's own auto-derived default)."))

    dlg.fields["kiss_icp_config"].trace_add("write", _refresh_config_voxel_size_label)
    _refresh_config_voxel_size_label()

    dlg.add_text_field("kiss_icp_voxel_size", "Voxel size override (m, optional):", default="")
    dlg.add_hint("Leave blank to use the config's own voxel_size, or kiss-icp's built-in "
                 "default if no config is given. Not yet confirmed against a real "
                 "installed kiss-icp version - if this errors, edit the config YAML's "
                 "mapping.voxel_size directly instead and leave this blank.")
    dlg.add_text_field("kiss_icp_dataloader", "Force dataloader (optional):", default="")
    dlg.add_text_field("kiss_icp_topic", "Rosbag topic (optional):", default="")
    dlg.add_hint("Dataloader: leave blank to auto-detect (ouster/rosbag/mcap/generic) - "
                 "override if it guesses wrong. Topic: only needed if a rosbag has more "
                 "than one PointCloud2 topic.")
    dlg.end_section()

    def _update_backend_sections(*_args):
        if dlg.fields["backend"].get() == "kiss_icp":
            ouster_section.grid_remove()
            kiss_section.grid()
        else:
            kiss_section.grid_remove()
            ouster_section.grid()

    dlg.fields["backend"].trace_add("write", _update_backend_sections)
    _update_backend_sections()


    def build():
        # Radio-button-backed now (add_radio_choice) - always exactly "ouster" or
        # "kiss_icp", never blank/typo'd, so no fallback is needed here anymore.
        backend = dlg.fields["backend"].get()
        source = dlg.require_existing_file("source", "Source")
        meta = dlg.fields["meta"].get().strip() or None
        output = dlg.require("output", "Output .ply")
        active_pipeline = dlg.get_active_pipeline_for_run()
        finish_info = {"pipeline": active_pipeline, "stage_name": "slam", "output": output}

        if backend == "kiss_icp":
            script = dlg.require("kiss_icp_script", "KISS-ICP script")
            config = dlg.fields["kiss_icp_config"].get().strip() or None
            dataloader = dlg.fields["kiss_icp_dataloader"].get().strip() or None
            topic = dlg.fields["kiss_icp_topic"].get().strip() or None
            voxel_size_text = dlg.fields["kiss_icp_voxel_size"].get().strip()
            if voxel_size_text:
                try:
                    kiss_voxel_size = float(voxel_size_text)
                except ValueError:
                    raise ValueError("Voxel size override must be a number, e.g. 0.08, or "
                                      "left blank to use the config's own value.")
            else:
                kiss_voxel_size = None
            cmd = core.build_kiss_icp_slam_command(
                script, source, output, config=config, dataloader=dataloader,
                topic=topic, meta=meta, voxel_size=kiss_voxel_size, pipeline=active_pipeline)

            effective_voxel_size = (kiss_voxel_size if kiss_voxel_size is not None
                                     else core.read_kiss_icp_voxel_size(config))
            report = (
                "=== SUMMARY ===\n"
                f"Backend: KISS-ICP\n"
                f"Source: {source}\n"
                + (f"Config: {config}\n" if config else
                   "No config given - using kiss-icp's vehicle-scale defaults, which will "
                   "likely under-populate an indoor map. Strongly consider adding one.\n")
                + (f"Voxel size override: {kiss_voxel_size} m\n" if kiss_voxel_size is not None
                   else f"Voxel size (from config): {effective_voxel_size} m\n"
                   if effective_voxel_size is not None
                   else "Voxel size: unknown (no override, and none read from the config - "
                        "kiss-icp's own auto-derived default applies)\n")
                + f"Saved to: {output}\n\n"
                "=== NOTE ===\n"
                "Check the tool output below for the actual point count - if it's in the "
                "low thousands for what should be a substantial capture, that's the "
                "no-config under-population issue, not a bug in this run.\n\n"
                "=== NEXT STEPS ===\n"
                "Open it in CloudCompare to check map quality, or take it into Stage 2 "
                "(Level) to correct any tilt before cleanup."
            )
            return cmd, report, finish_info

        try:
            voxel_size = float(dlg.fields["voxel_size"].get())
        except ValueError:
            raise ValueError("Voxel size must be a number, e.g. 0.25")
        visualize = dlg.fields["visualize"].get()
        cmd = core.build_slam_command(source, voxel_size, output, meta=meta,
                                       visualize=visualize, pipeline=active_pipeline)

        report = (
            "=== SUMMARY ===\n"
            f"Backend: Ouster CLI\n"
            f"Source: {source}\n"
            + (f"Meta: {meta}\n" if meta else "No meta file given - resolved from the source itself.\n")
            + f"Saved to: {output}\n"
            f"Voxel size used: {voxel_size} m\n"
            f"Visualizer: {'opened' if visualize else 'not opened'}\n\n"
            "=== NEXT STEPS ===\n"
            "Open it in CloudCompare to check map quality, "
            "or take it into Stage 2 (Level) to correct any tilt before cleanup."
        )
        return cmd, report, finish_info

    dlg.add_run_button(build)
    return dlg


def open_level_dialog(parent, run_callback, pipeline=None):
    dlg = StageDialog(parent, "Stage 2: Level", run_callback)
    pipeline = pipeline if (pipeline is not None and pipeline.kind in ("baseline", "scan")) else None
    dlg.pipeline = pipeline

    if pipeline is not None:
        dlg.add_pipeline_label(pipeline)

    default_script = str(Path(__file__).resolve().parent / "level_cloud.py")
    dlg.add_file_field("script", "Level script (.py):", [("Python files", "*.py")],
                        default=default_script)
    dlg.add_file_field("input", "Raw SLAM output .ply:", [("PLY files", "*.ply")])
    if pipeline is not None:
        dlg.add_project_picker_button(
            "input", pipeline.project, lambda: pm.list_eligible_inputs(pipeline, "level"))

    output_default = dlg.resolve_project_output_default(pipeline, "level", ".ply")
    dlg.add_save_field("output", "Output .ply:", default_ext=".ply", default=output_default)

    dlg.add_hint("ouster-cli's SLAM has no gravity/leveling step, so the map inherits "
                 "whatever tilt the sensor had at frame one. This finds the floor via "
                 "RANSAC and levels the cloud to Z=0 - worth doing before Stage 3's ICP "
                 "alignment, which works better on an already-level cloud.")

    # Stage 1's actual voxel size, read back from what start_stage() recorded for THIS
    # pipeline's "slam" stage - works for either backend, since build_kiss_icp_slam_command
    # now records the same "voxel_size" key Ouster CLI always has (its override if one was
    # given, else whatever was read from its config, else None if genuinely unknown - e.g.
    # KISS-ICP with no config at all). Without this, there was no way to know what Stage 1
    # actually used when KISS-ICP was the backend, since unlike Ouster CLI's own always-
    # visible voxel size field, KISS-ICP's only lives inside a config file.
    slam_params = {}
    if pipeline is not None:
        slam_params = ((pipeline.entry.get("stages", {}) or {}).get("slam", {}) or {}).get(
            "params", {}) or {}
    stage1_voxel_size = slam_params.get("voxel_size")
    stage1_backend_label = "KISS-ICP" if slam_params.get("backend") == "kiss_icp" else "Ouster CLI"

    dlg.add_preset_selector("Distance threshold preset:", [
        ("Tight (0.02m) - script default, needs fine/dense data", {"distance_threshold": "0.02"}),
        ("Medium (0.1m) - reasonable if Stage 1 voxel size was ~0.1-0.15m", {"distance_threshold": "0.1"}),
        ("Loose (0.25m) - try if 'no planes found', or Stage 1 voxel size was ~0.25m+", {"distance_threshold": "0.25"}),
    ])
    distance_threshold_default = (
        f"{stage1_voxel_size}" if stage1_voxel_size is not None else "0.02")
    dlg.add_text_field("distance_threshold", "Distance threshold (m):",
                        default=distance_threshold_default)
    if stage1_voxel_size is not None:
        voxel_source_note = (
            f" This pipeline's Stage 1 ({stage1_backend_label}) used voxel_size = "
            f"{stage1_voxel_size} m, recorded in project.json - the field above starts "
            f"there, matching it.")
    elif pipeline is not None and slam_params.get("backend") == "kiss_icp":
        voxel_source_note = (
            " This pipeline's Stage 1 used KISS-ICP with no voxel_size found (no config "
            "given, or none read from it) - kiss-icp's own auto-derived default applied, "
            "so there's nothing specific to match here; the field above kept the script's "
            "own 0.02 default.")
    else:
        voxel_source_note = ""
    dlg.add_hint("RANSAC plane-fit tolerance. 'No planes found' almost always means "
                 "this is too tight - raise it to roughly match or exceed Stage 1's "
                 "voxel size, since coarser voxels sit further from a perfect plane."
                 + voxel_source_note)
    dlg.add_text_field("max_planes", "Max planes to search:", default="6")
    dlg.add_text_field("min_inlier_fraction", "Min plane size (fraction of all points):", default="0.02")
    dlg.add_hint("Lower this if a real floor/wall is smaller relative to the whole "
                 "cloud than usual (e.g. a small compartment scanned with a lot of "
                 "surrounding clutter).")

    dlg.add_text_field("horizontal_threshold", "Horizontal threshold:", default="0.7")
    dlg.add_hint("How close to vertical (|normal.z|) a candidate plane must be to even "
                 "be eligible as the floor. The lowest candidate that clears this bar is "
                 "picked as the floor, not the biggest - a real ceiling can have MORE "
                 "points than a real floor (cleaner surface, less clutter), so the "
                 "biggest-plane-wins approach this script used to use could pick the "
                 "ceiling by mistake, confirmed on real data. Only loosen this (lower it) "
                 "if a real floor/ceiling isn't clearing the bar at all - check the run "
                 "report's candidate list for its horizontality value first.")


    def build():
        script = dlg.require("script", "Level script")
        input_ply = dlg.require("input", "Raw SLAM output .ply")
        output = dlg.require("output", "Output .ply")
        try:
            distance_threshold = float(dlg.fields["distance_threshold"].get())
            max_planes = int(dlg.fields["max_planes"].get())
            min_inlier_fraction = float(dlg.fields["min_inlier_fraction"].get())
            horizontal_threshold = float(dlg.fields["horizontal_threshold"].get())
        except ValueError:
            raise ValueError("Distance threshold/min plane size/horizontal threshold "
                              "must be numbers, max planes must be a whole number.")
        active_pipeline = dlg.get_active_pipeline_for_run()
        finish_info = {"pipeline": active_pipeline, "stage_name": "level", "output": output}
        cmd = core.build_level_command(script, input_ply, output,
                                        distance_threshold=distance_threshold,
                                        max_planes=max_planes,
                                        min_inlier_fraction=min_inlier_fraction,
                                        horizontal_threshold=horizontal_threshold,
                                        pipeline=active_pipeline)

        report = (
            "=== SUMMARY ===\n"
            f"Input: {input_ply}\n"
            f"Saved to: {output}\n"
            f"Distance threshold: {distance_threshold} m\n"
            f"Max planes searched: {max_planes}\n"
            f"Min plane size: {min_inlier_fraction * 100:.1f}% of all points\n"
            f"Horizontal threshold: {horizontal_threshold}\n\n"
            "=== NOTE ===\n"
            "The tool output below lists each candidate plane it found, its Z "
            "position, and which one it picked as the floor - the LOWEST candidate "
            "that clears the horizontal threshold, not the biggest one (a real "
            "ceiling can have more points than a real floor). If the chosen floor "
            "has very few points, or the console shows a low-horizontality warning "
            "or a fallback warning, check the leveled result visually in "
            "CloudCompare before trusting it. If it found no planes at all, try a "
            "larger distance threshold above and re-run.\n\n"
            "=== NEXT STEPS ===\n"
            "Open the leveled cloud in CloudCompare to confirm the floor actually "
            "looks horizontal now. Then use it as the input to Stage 3 (Cleanup)."
        )
        return cmd, report, finish_info

    dlg.add_run_button(build)
    return dlg


def open_cleanup_dialog(parent, run_callback, pipeline=None, project=None):
    dlg = StageDialog(parent, "Stage 3: Cleanup", run_callback)
    pipeline = pipeline if (pipeline is not None and pipeline.kind in ("baseline", "scan")) else None
    dlg.pipeline = pipeline

    if pipeline is not None:
        dlg.add_pipeline_label(pipeline)

    dlg.add_file_field("input", "Input .ply:", [("PLY files", "*.ply")])
    if pipeline is not None:
        dlg.add_project_picker_button(
            "input", pipeline.project, lambda: pm.list_eligible_inputs(pipeline, "cleanup"))
    dlg.add_preset_selector("SOR preset:", [
        ("Conservative - keeps more points, safer on sparse scans",
         {"sor_neighbors": "10", "sor_std_dev": "2.0"}),
        ("Balanced - good default", {"sor_neighbors": "6", "sor_std_dev": "1.0"}),
        ("Aggressive - removes more noise, risk of losing real detail",
         {"sor_neighbors": "6", "sor_std_dev": "0.5"}),
    ])
    dlg.add_text_field("sor_neighbors", "SOR neighbors:", default="6")
    dlg.add_text_field("sor_std_dev", "SOR std dev:", default="1.0")
    dlg.add_hint("Neighbors = how many nearby points are checked per point (higher = "
                 "smoother, slower). Std dev = outlier cutoff in standard deviations "
                 "(lower = more aggressive). For precise tuning, CloudCompare's own SOR "
                 "dialog shows a live preview - dial in there, then reuse the numbers here.")
    dlg.add_file_field("align_to", "Align to (optional baseline .ply):", [("PLY files", "*.ply")])

    if pipeline is not None and pipeline.kind == "scan" and project is not None:
        def use_project_baseline():
            try:
                path = pm.get_baseline_cleanup_output(project)
            except pm.ProjectError as e:
                messagebox.showinfo("Baseline not ready", str(e))
                return
            dlg.fields["align_to"].set(path)

        ttk.Button(dlg.form, text="Use Project Baseline (Cleanup output)",
                   command=use_project_baseline).grid(
            row=dlg.row, column=0, columnspan=3, sticky="w", pady=(0, 4))
        dlg.row += 1
        dlg.add_hint("Fills 'Align to' with the project's own baseline cleanup output - "
                      "the usual choice for a comparison scan, since this is what makes "
                      "a later 'vs. baseline' diff meaningful.")

    dlg.add_registered_baseline_preset("align_to")
    dlg.add_hint("Optional. Runs ICP to fine-align onto the baseline after cleanup - use "
                 "when comparing a later scan. Leave blank when this cloud IS the "
                 "baseline you're creating.")
    dlg.add_save_field("output", "Output .ply:", default_ext=".ply")
    dlg.add_hint("The name you want the cleaned result saved as. CloudCompare names the "
                 "file itself when it runs - it gets detected and renamed to this "
                 "automatically afterward (a more direct naming flag was tried and "
                 "proved unreliable in testing).")
    dlg.add_text_field("register_as", "Also register this output as a manual-mode baseline for:", default="")
    dlg.add_hint("Optional, separate from project mode. Registers this output as the "
                 "active entry in baseline_registry.json (selectable above and in Stage "
                 "5's manual mode) - for manual-mode work outside a project. In project "
                 "mode, the baseline pipeline itself already is the record that matters.")

    output_default = dlg.resolve_project_output_default(pipeline, "cleanup", ".ply")
    if output_default:
        dlg.fields["output"].set(output_default)


    def build():
        input_ply = dlg.require("input", "Input .ply")
        output = dlg.require("output", "Output .ply")
        align_to = dlg.fields["align_to"].get().strip() or None
        if align_to and not Path(align_to).exists():
            raise ValueError(
                f"'Align to' points at a file that doesn't exist:\n{align_to}\n\n"
                "If you picked this from a preset dropdown, that entry may be "
                "stale (the file was moved/renamed/deleted since it was "
                "registered). Either fix the path or clear the field."
            )
        register_as = dlg.fields["register_as"].get().strip() or None
        try:
            neighbors = int(dlg.fields["sor_neighbors"].get())
            std_dev = float(dlg.fields["sor_std_dev"].get())
        except ValueError:
            raise ValueError("SOR neighbors must be an integer and std dev a number.")

        active_pipeline = dlg.get_active_pipeline_for_run()

        # CloudCompare doesn't auto-write a distinct RMS/ICP report file -
        # its stats only appear in the console unless we ask for -LOG_FILE.
        # This path is one we choose, so we know exactly where to look.
        log_path = Path(output).with_name(Path(output).stem + "_cc_log.txt")
        cmd = core.build_cleanup_command(input_ply, output, neighbors, std_dev,
                                          align_to, log_file=log_path, pipeline=active_pipeline)

        # Snapshot .ply files now, before running, so the actual output can
        # be identified afterward by what's new - see resolve_cleanup_output.
        # This watches INPUT's own folder, not the desired OUTPUT's folder:
        # CloudCompare's -SAVE_CLOUDS saves each loaded cloud next to itself
        # (it has no concept of "the folder the caller wants"), the same
        # behavior Stage 5 (Diff)'s own snapshot already accounts for by
        # watching the comparison cloud's folder, not the desired output's.
        # Watching the wrong folder here meant a real, successful Cleanup
        # run could still find "no new .ply files" (since none appeared in
        # the folder being watched) and silently record a stage as complete
        # pointing at a file that was never actually written - confirmed
        # against a real run's CloudCompare log, where the SOR-filtered
        # result landed in Stage 2 (Level)'s own output folder (the input's
        # folder), not Stage 3 (Cleanup)'s.
        input_dir = Path(input_ply).resolve().parent
        existing_ply_before = set(input_dir.glob("*.ply")) if input_dir.is_dir() else set()

        resolved_state = {}

        def build_report():
            summary = (
                "=== SUMMARY ===\n"
                f"Input cleaned: {input_ply}\n"
                f"SOR settings: {neighbors} neighbors, {std_dev} std dev\n"
                + (f"Aligned (ICP) to: {align_to}\n" if align_to else "No alignment performed (no baseline given).\n")
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
                    messagebox.showwarning(
                        "Registration RMS not found",
                        "Couldn't find an RMS value in this run's CloudCompare log.\n\n"
                        "This means Stage 5 (Diff)'s M3C2 significance test won't have "
                        "a real registration-error figure to work with. Leave that "
                        "field blank there rather than entering 0 - a 0 collapses "
                        "M3C2's Level of Detection calculation, which flags almost "
                        "every point as significant, which is worse than not running "
                        "the significance test at all."
                    )
                    summary += (
                        "\n=== REGISTRATION RMS ===\n"
                        "Not found in the CloudCompare log - the significance test "
                        "in Stage 5 (Diff) won't have a real registration-error value "
                        "to use. Leave that field blank there rather than entering 0."
                    )
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
                        f"pass/fail number on its own."
                    )

            if register_as and resolved:
                core.register_baseline(register_as, resolved)
                summary += (
                    f"\nRegistered as the manual-mode active baseline for '{register_as}' - "
                    f"pick it from the dropdown in Stage 3/5 next time instead of "
                    f"browsing to this file.\n"
                )
            elif register_as and not resolved:
                summary += (
                    f"\nCouldn't register as baseline - the output file couldn't be "
                    f"confidently identified (see OUTPUT section above).\n"
                )

            summary += (
                "\n=== NEXT STEPS ===\n"
                "Open the cleaned cloud in CloudCompare to confirm alignment, "
                "then use it (plus a matching cleaned baseline/comparison) in Stage 5 (Diff)."
            )

            annotated = core.annotate_log_file(log_path, "Stage 3: Cleanup", cmd)
            if annotated:
                resolved_state["log_path"] = log_path
                summary += (
                    f"\n\n=== CLOUDCOMPARE LOG (with headers added) ===\n"
                    f"Saved to: {log_path}\n"
                    "This file now has clear section headers wrapped around "
                    "CloudCompare's raw output (RMS/fitness stats for ICP runs "
                    "are in there) - open it directly for the full detail."
                )
            else:
                summary += (
                    "\n\n=== CLOUDCOMPARE LOG ===\n"
                    "No log file was found at the expected path - check the "
                    "main window log for RMS/alignment stats instead."
                )
            return summary

        finish_info = {"pipeline": active_pipeline, "stage_name": "cleanup",
                        "output": output, "resolve_state": resolved_state}
        return cmd, build_report, finish_info

    dlg.add_run_button(build)
    return dlg


def _default_segment_output_dir(pipeline):
    """Computes a fresh sequence-numbered folder name for Stage 4
    (Segment)'s output, e.g. 'compartment_04_segment_001' - the SAME
    naming convention every other stage's project_manager.get_output_path()
    already uses (PROJECT_SCHEMA_v2.md Section 13.1), just applied to a
    folder instead of a single file, since segment_planes.py writes
    several files together into one folder (Section 13.3).

    get_output_path() itself isn't reused here: its own sequence counter
    globs for FILES ending in a given extension directly inside the stage
    folder, which would never see a past run's own subfolder (a directory
    has no matching extension) - every call would keep returning '001'
    forever. This counts existing subfolders directly instead, so a
    re-run gets its own, non-colliding numbered folder, same as every
    other stage's output naming.

    Returns "" if pipeline is None (manual mode - no project folder to
    scan, same as every other project-mode-only default in this file)."""
    if pipeline is None:
        return ""
    folder = pipeline.root / pipeline.stage_folders["segment"]
    compartment = pipeline.project.data["compartment"]
    prefix = f"{compartment}_segment_"
    existing_numbers = []
    if folder.is_dir():
        for entry in folder.iterdir():
            if entry.is_dir() and entry.name.startswith(prefix):
                stem = entry.name[len(prefix):]
                if stem.isdigit():
                    existing_numbers.append(int(stem))
    next_seq = (max(existing_numbers) + 1) if existing_numbers else 1
    return str(folder / f"{prefix}{next_seq:03d}")


def open_segment_dialog(parent, run_callback, pipeline=None):
    dlg = StageDialog(parent, "Stage 4: Segment", run_callback)
    pipeline = pipeline if (pipeline is not None and pipeline.kind in ("baseline", "scan")) else None
    dlg.pipeline = pipeline

    if pipeline is not None:
        dlg.add_pipeline_label(pipeline)

    default_script = str(Path(__file__).resolve().parent / "segment_planes.py")
    dlg.add_file_field("script", "Segment script (.py):", [("Python files", "*.py")],
                        default=default_script)
    dlg.add_file_field("input", "Cleaned .ply (ideally leveled first):", [("PLY files", "*.ply")])
    if pipeline is not None:
        dlg.add_project_picker_button(
            "input", pipeline.project, lambda: pm.list_eligible_inputs(pipeline, "segment"))

    # Output is a FOLDER, not a single file (PROJECT_SCHEMA_v2.md Section
    # 13.3) - segment_planes.py writes several files together (one cloud
    # per detected surface, a combined <name>_classified.ply, a
    # <name>_envelope.ply, a <name>_unclassified.ply, and a manifest.json)
    # into one directory, so this needs its own folder-picker field rather
    # than add_save_field (which is built around picking a single file's
    # save location). <name> is the output folder's own name (e.g.
    # compartment_04_segment_001), so files stay distinguishable from an
    # earlier run's even if opened outside their folder - manifest.json
    # keeps its fixed, unprefixed name since it's a machine-read sidecar,
    # not something opened directly in a viewer.
    ttk.Label(dlg.form, text="Output folder:").grid(row=dlg.row, column=0, sticky="w", pady=3)
    output_dir_var = tk.StringVar(value=_default_segment_output_dir(pipeline))
    ttk.Entry(dlg.form, textvariable=output_dir_var, width=55).grid(row=dlg.row, column=1, padx=5)

    def browse_output_dir():
        path = filedialog.askdirectory(parent=dlg)
        if path:
            output_dir_var.set(path)

    ttk.Button(dlg.form, text="Browse...", command=browse_output_dir).grid(row=dlg.row, column=2)
    dlg.fields["output_dir"] = output_dir_var
    dlg.row += 1
    dlg.add_hint("A fresh, numbered folder each run. <name>_classified.ply is what carries "
                 "forward as this stage's output.")

    dlg.add_checkbox("write_separate_surfaces",
                      "Also write each surface as its own separate .ply file", default=False)
    dlg.add_hint("Off by default. Turn on for extra per-surface .ply files (floor, wall_1, "
                 "...) alongside classified.ply - useful for Omniverse or hand-tuning.")

    dlg.add_preset_selector("Distance threshold preset:", [
        ("Default (0.05m, 20 max planes) - tested combo for a full room/compartment scan",
         {"distance_threshold": "0.05", "max_planes": "20", "min_inlier_fraction": "0.003",
          "cluster_eps": "0.5"}),
        ("Tight (0.02m) - matches level_cloud.py's own script default", {"distance_threshold": "0.02"}),
        ("Medium (0.1m) - try if a real wall/floor doesn't survive as its own plane",
         {"distance_threshold": "0.1"}),
        ("Loose (0.25m)", {"distance_threshold": "0.25"}),
    ])
    dlg.add_text_field("distance_threshold", "Distance threshold (m):", default="0.05")
    dlg.add_hint("RANSAC plane-fit tolerance - match or exceed Stage 1's voxel size.")
    dlg.add_text_field("max_planes", "Max planes to search:", default="20")
    dlg.add_text_field("min_inlier_fraction", "Min plane size (fraction of all points):", default="0.003")
    dlg.add_hint("Minimum fraction of all points a plane needs to be accepted. Raise if "
                 "small spurious surfaces are getting through.")
    dlg.add_text_field("horizontal_threshold", "Horizontal threshold:", default="0.7")
    dlg.add_text_field("max_horizontal_z_span", "Max horizontal Z span (m):", default="0.3")
    dlg.add_hint("Horizontal threshold: how flat a plane must be (1.0 = exactly) to count "
                 "as floor/ceiling rather than a wall. Max Z span: rejects a near-horizontal "
                 "candidate that spans too much height to be a real floor/ceiling.")

    dlg.add_checkbox("cluster_filter", "Filter each surface to its largest connected cluster", default=True)
    dlg.add_text_field("cluster_eps", "Cluster gap tolerance (m):", default="0.5")
    dlg.add_text_field("cluster_min_points", "Cluster min points:", default="20")
    dlg.add_hint("Keeps only each plane's largest connected cluster (DBSCAN), moving "
                 "disconnected stray points to 'unclassified'. Ignored if unchecked.")

    dlg.add_checkbox("merge_coplanar", "Merge split detections of the same physical plane", default=True)
    dlg.add_text_field("merge_normal_cos", "Merge normal similarity (1.0 = exactly parallel):", default="0.98")
    dlg.add_text_field("merge_distance", "Merge plane distance (m):", default="0.1")
    dlg.add_hint("Merges a wall/ceiling that clutter split into two detections back into "
                 "one. Ignored if unchecked.")

    dlg.add_checkbox("envelope_filter",
                      "Split 'unclassified' into interior clutter vs. outside-the-room junk",
                      default=True)
    dlg.add_text_field("envelope_margin", "Envelope margin (m):", default="0.15")
    dlg.add_checkbox("write_envelope_filtered",
                      "Also write a ready-to-use junk-removed .ply (envelope_filtered)",
                      default=True)
    dlg.add_hint("Flags unclassified points outside the room's derived footprint as likely "
                 "junk. classified.ply always keeps every point either way (via "
                 "'outside_envelope'); the checkbox controls whether a pre-filtered copy "
                 "also gets written. Margin: slack before something counts as outside.")

    def build():
        script = dlg.require("script", "Segment script")
        input_ply = dlg.require("input", "Cleaned .ply")
        output_dir = dlg.require("output_dir", "Output folder")
        try:
            distance_threshold = float(dlg.fields["distance_threshold"].get())
            max_planes = int(dlg.fields["max_planes"].get())
            min_inlier_fraction = float(dlg.fields["min_inlier_fraction"].get())
            horizontal_threshold = float(dlg.fields["horizontal_threshold"].get())
            max_horizontal_z_span = float(dlg.fields["max_horizontal_z_span"].get())
        except ValueError:
            raise ValueError("Distance threshold, min plane size, horizontal threshold, and "
                              "max horizontal Z span must be numbers; max planes must be a "
                              "whole number.")
        cluster_filter = dlg.fields["cluster_filter"].get()
        merge_coplanar = dlg.fields["merge_coplanar"].get()
        write_separate_surfaces = dlg.fields["write_separate_surfaces"].get()
        envelope_filter = dlg.fields["envelope_filter"].get()
        write_envelope_filtered = dlg.fields["write_envelope_filtered"].get()
        try:
            cluster_eps = float(dlg.fields["cluster_eps"].get())
            cluster_min_points = int(dlg.fields["cluster_min_points"].get())
            merge_normal_cos = float(dlg.fields["merge_normal_cos"].get())
            merge_distance = float(dlg.fields["merge_distance"].get())
            envelope_margin = float(dlg.fields["envelope_margin"].get())
        except ValueError:
            raise ValueError("Cluster gap tolerance, merge normal similarity, merge plane "
                              "distance, and envelope margin must be numbers; cluster min "
                              "points must be a whole number.")

        active_pipeline = dlg.get_active_pipeline_for_run()
        cmd = core.build_segment_command(
            script, input_ply, output_dir,
            distance_threshold=distance_threshold, max_planes=max_planes,
            horizontal_threshold=horizontal_threshold, max_horizontal_z_span=max_horizontal_z_span,
            min_inlier_fraction=min_inlier_fraction, cluster_filter=cluster_filter,
            cluster_eps=cluster_eps, cluster_min_points=cluster_min_points,
            merge_coplanar=merge_coplanar, merge_normal_cos=merge_normal_cos,
            merge_distance=merge_distance, write_separate_surfaces=write_separate_surfaces,
            envelope_filter=envelope_filter, envelope_margin=envelope_margin,
            write_envelope_filtered=write_envelope_filtered,
            pipeline=active_pipeline)

        resolved_state = {}

        def build_report():
            summary = (
                "=== SUMMARY ===\n"
                f"Input: {input_ply}\n"
                f"Output folder: {output_dir}\n"
                f"Distance threshold: {distance_threshold} m, max planes: {max_planes}\n"
            )

            classified_path, extra_fields = core.resolve_segment_output(output_dir)
            if classified_path is None:
                summary += (
                    "\n=== OUTPUT ===\n"
                    "No manifest.json found in the output folder, or it had no usable "
                    "classified-cloud record - the run likely failed before writing one. "
                    "Check the main window log above for the actual error.\n"
                )
            else:
                resolved_state["output"] = classified_path
                if active_pipeline is not None:
                    converted = {}
                    if "envelope_output" in extra_fields:
                        try:
                            converted["envelope_output"] = pm.to_relative_path(
                                active_pipeline.project, extra_fields["envelope_output"])
                        except pm.ProjectError:
                            converted["envelope_output"] = extra_fields["envelope_output"]
                    if "envelope_filtered_output" in extra_fields:
                        try:
                            converted["envelope_filtered_output"] = pm.to_relative_path(
                                active_pipeline.project, extra_fields["envelope_filtered_output"])
                        except pm.ProjectError:
                            converted["envelope_filtered_output"] = extra_fields["envelope_filtered_output"]
                    if "n_outside_envelope" in extra_fields:
                        converted["n_outside_envelope"] = extra_fields["n_outside_envelope"]
                    if "classification_ids" in extra_fields:
                        converted["classification_ids"] = extra_fields["classification_ids"]
                    if "surfaces" in extra_fields:
                        converted_surfaces = []
                        for surface in extra_fields["surfaces"]:
                            surface = dict(surface)
                            if surface.get("file"):
                                try:
                                    surface["file"] = pm.to_relative_path(
                                        active_pipeline.project, surface["file"])
                                except pm.ProjectError:
                                    pass
                            converted_surfaces.append(surface)
                        converted["surfaces"] = converted_surfaces
                    resolved_state["extra_fields"] = converted

                surface_names = [s["name"] for s in extra_fields.get("surfaces", [])]
                summary += (
                    f"\nFound {len(surface_names)} surface(s): {', '.join(surface_names) or '(none)'}\n"
                    f"Combined classified cloud saved to: {classified_path}\n"
                )
                if "envelope_output" in extra_fields:
                    summary += (f"Envelope-only cloud (ready-made input for Stage 7/Surface's "
                                f"unified shell reconstruction): {extra_fields['envelope_output']}\n")
                if "n_outside_envelope" in extra_fields:
                    summary += (f"{extra_fields['n_outside_envelope']} unclassified point(s) flagged "
                                f"as outside the room's derived footprint/height range (likely scan "
                                f"noise/junk beyond the walls, not real interior content).\n")
                if "envelope_filtered_output" in extra_fields:
                    summary += (f"Junk-removed cloud (classified.ply minus those flagged points): "
                                f"{extra_fields['envelope_filtered_output']}\n")

            summary += (
                "\n=== NEXT STEPS ===\n"
                f"Open {Path(classified_path).name if classified_path else 'the classified cloud'} "
                "in CloudCompare and color by the 'classification' field to check the surfaces "
                "look right - a wall misclassified as ceiling (or vice versa) is the first thing "
                "to check against the surface list above. If a real wall seems to be missing, "
                "check the unclassified cloud (same output folder, "
                "'<name>_unclassified.ply' - only written when 'Also write each surface as its "
                "own separate .ply file' is checked) for a flat-ish cluster of points rather "
                "than a scattered one - that's usually it, broken up by clutter or debris stuck "
                "to it."
            )
            return summary

        finish_info = {"pipeline": active_pipeline, "stage_name": "segment",
                        "output": output_dir, "resolve_state": resolved_state}
        return cmd, build_report, finish_info

    dlg.add_run_button(build)
    return dlg


# ---------------------------------------------------------------------------
# Stage-specific dialogs: Stage 5-8 (diff pipelines)
# ---------------------------------------------------------------------------

def open_diff_dialog(parent, run_callback, pipeline=None):
    dlg = StageDialog(parent, "Stage 5: Diff (M3C2)", run_callback)
    pipeline = pipeline if (pipeline is not None and pipeline.kind == "diff") else None
    dlg.pipeline = pipeline

    if pipeline is not None:
        dlg.add_pipeline_label(pipeline)
        diff_entry = pipeline.entry
        ttk.Label(dlg.form,
                  text=f"(reference={diff_entry['reference']}, comparison={diff_entry['comparison']})",
                  foreground="#777", font=("Segoe UI", 8)
                  ).grid(row=dlg.row, column=0, columnspan=3, sticky="w", pady=(0, 6))
        dlg.row += 1

    dlg.add_file_field("baseline", "Baseline .ply:", [("PLY files", "*.ply")])
    if pipeline is not None:
        dlg.add_project_picker_button(
            "baseline", pipeline.project,
            lambda: pm.list_side_candidates(pipeline.project, pipeline.entry["reference"]))
    dlg.add_registered_baseline_preset("baseline")
    dlg.add_file_field("comparison", "Comparison .ply:", [("PLY files", "*.ply")])
    if pipeline is not None:
        def _fill_rms_from_pick(path):
            rms = pm.find_icp_rms_for_path(pipeline.project, path)
            if rms is not None:
                dlg.fields["registration_rms"].set(f"{rms:.6f}")

        dlg.add_project_picker_button(
            "comparison", pipeline.project,
            lambda: pm.list_side_candidates(pipeline.project, pipeline.entry["comparison"]),
            extra_on_pick=_fill_rms_from_pick)

    ttk.Label(dlg.form, text="Registration RMS (from Stage 3):").grid(
        row=dlg.row, column=0, sticky="w", pady=3)
    rms_var = tk.StringVar()
    ttk.Entry(dlg.form, textvariable=rms_var, width=20).grid(
        row=dlg.row, column=1, sticky="w", padx=5)
    dlg.fields["registration_rms"] = rms_var

    def load_rms():
        comparison_path = dlg.fields["comparison"].get().strip()
        if not comparison_path:
            messagebox.showinfo("No comparison file", "Fill in the Comparison .ply field first.")
            return
        sidecar = core.load_rms_sidecar(comparison_path)
        if sidecar and sidecar.get("rms") is not None:
            rms_var.set(f"{sidecar['rms']:.6f}")
        else:
            messagebox.showinfo(
                "No RMS found",
                f"No saved RMS for:\n{comparison_path}\n\n"
                "This is only recorded when Stage 3 (Cleanup) ran with an "
                "'Align to baseline' target and successfully found an RMS "
                "value in the CloudCompare log."
            )

    ttk.Button(dlg.form, text="Load from Stage 3", command=load_rms).grid(
        row=dlg.row, column=2, padx=5)
    dlg.row += 1
    dlg.add_hint("Feeds M3C2's Level of Detection (LOD) calculation - separates real "
                 "change from noise/misalignment. Leave blank if unknown, not 0: a 0 "
                 "collapses the LOD and flags almost everything as significant.")

    range_choice_var = tk.StringVar(value="min")

    def check_point_spacing():
        baseline_path = dlg.fields["baseline"].get().strip()
        if not baseline_path or not Path(baseline_path).exists():
            messagebox.showerror("No baseline file",
                                  "Fill in a valid Baseline .ply file first (point spacing "
                                  "is checked against whatever's in that field).")
            return
        script_path = Path(__file__).resolve().parent / "point_spacing.py"
        if not script_path.exists():
            messagebox.showerror("Missing script",
                                  f"point_spacing.py not found next to the applet:\n{script_path}")
            return

        dlg.config(cursor="watch")
        dlg.update_idletasks()
        try:
            result = subprocess.run(
                [sys.executable, str(script_path), "--input", baseline_path],
                capture_output=True, text=True, timeout=120)
        except Exception as e:
            dlg.config(cursor="")
            messagebox.showerror("Error running point_spacing.py", str(e))
            return
        dlg.config(cursor="")

        output = result.stdout + (("\n" + result.stderr) if result.stderr else "")
        if result.returncode != 0:
            messagebox.showerror("point_spacing.py failed", output or "No output captured.")
            return

        match = re.search(r"Suggested M3C2 normal diameter range:\s*([\d.]+)\s*to\s*([\d.]+)", output)
        if not match:
            messagebox.showinfo("Point Spacing (Baseline)",
                                 output + "\n\n(Couldn't find a suggested range in the "
                                          "output to auto-fill from - fill in the fields "
                                          "manually using the stats above.)")
            return

        low, high = float(match.group(1)), float(match.group(2))
        choice = range_choice_var.get()
        normal_scale = {"min": low, "mid": (low + high) / 2, "max": high}[choice]

        # Ratios confirmed exactly from a real working reference params file
        # (SearchScale = 0.5x, SearchDepth = 2x NormalScale) - see
        # generate_m3c2_params.py for the same reference.
        search_scale = normal_scale * 0.5
        search_depth = normal_scale * 2.0

        dlg.fields["normal_scale"].set(f"{normal_scale:.4f}")
        dlg.fields["search_scale"].set(f"{search_scale:.4f}")
        dlg.fields["search_depth"].set(f"{search_depth:.4f}")

        output += (
            f"\n\nFilled in ({choice} of range):\n"
            f"  Normal scale: {normal_scale:.4f}\n"
            f"  Search scale: {search_scale:.4f}  (0.5x normal)\n"
            f"  Search depth: {search_depth:.4f}  (2x normal)\n"
            f"These overwrite whatever was already in those three fields."
        )
        messagebox.showinfo("Point Spacing (Baseline)", output)

    spacing_row_frame = ttk.Frame(dlg.form)
    spacing_row_frame.grid(row=dlg.row, column=0, columnspan=3, sticky="w", pady=(4, 0))
    ttk.Button(spacing_row_frame, text="Check Point Spacing (Baseline)...",
               command=check_point_spacing).pack(side="left")
    ttk.Label(spacing_row_frame, text="  Use:").pack(side="left")
    for value, text in [("min", "Min"), ("mid", "Mid"), ("max", "Max")]:
        ttk.Radiobutton(spacing_row_frame, text=text, value=value,
                         variable=range_choice_var).pack(side="left")
    dlg.row += 1
    dlg.add_hint("Min = tightest scale (best for small damage/sharp edges, noisier near "
                 "raw point spacing). Max = smoothest (safest against noise, but blunts "
                 "features - caused the earlier rounded-edges result). Mid is a balanced "
                 "default. Search scale/depth auto-fill as 0.5x/2x of normal scale.")

    dlg.add_text_field("normal_scale", "Normal scale (diameter):", default="")
    dlg.add_text_field("search_scale", "Search/projection scale (diameter):", default="")
    dlg.add_text_field("search_depth", "Search depth (max depth):", default="")
    dlg.add_hint("Auto-filled by 'Check Point Spacing' above, or set manually / via "
                 "CloudCompare's own 'Guess params'. If you edit Normal scale by hand "
                 "afterward, update these two to match - CloudCompare's 'Guess params' "
                 "does NOT do this automatically (caused a rounded/blunted-edges result "
                 "before this was caught).")

    def generate_params():
        try:
            normal_scale = float(dlg.fields["normal_scale"].get())
            search_scale = float(dlg.fields["search_scale"].get())
            search_depth = float(dlg.fields["search_depth"].get())
        except ValueError:
            messagebox.showerror("Missing values",
                                  "Fill in Normal scale, Search scale, and Search depth "
                                  "as numbers before generating.")
            return
        reg_rms = dlg.fields["registration_rms"].get().strip()
        if not reg_rms:
            messagebox.showerror("Missing registration error",
                                  "Fill in Registration RMS first (or click 'Load from "
                                  "Stage 3') - a blank/zero value defeats the significance test.")
            return
        try:
            reg_rms_val = float(reg_rms)
        except ValueError:
            messagebox.showerror("Invalid value", "Registration RMS must be a number.")
            return

        active_pipeline = dlg.get_active_pipeline_for_run()
        auto_named = False
        if active_pipeline is not None:
            # Project mode: name and place the file the same way every other
            # stage output is named (PROJECT_SCHEMA_v2.md Section 13.1's
            # numbered-file convention), inside this diff's own output
            # folder, instead of asking where to save it. The .txt sequence
            # counts separately from the .ply sequence in that same folder
            # (get_output_path only counts existing files matching the
            # extension it was asked for), so this does not collide with
            # the diff .ply's own numbering.
            save_path = pm.get_absolute_path(
                active_pipeline.project, pm.get_output_path(active_pipeline, "diff", ".txt"))
            auto_named = True
        else:
            save_path = filedialog.asksaveasfilename(
                parent=dlg, defaultextension=".txt", initialfile="m3c2_params.txt",
                filetypes=[("Text files", "*.txt")])
            if not save_path:
                return

        core.generate_m3c2_params_file(save_path, normal_scale, search_scale,
                                        search_depth, reg_rms_val)
        dlg.fields["params"].set(save_path)
        messagebox.showinfo(
            "Params file generated",
            f"Saved to:\n{save_path}\n\n"
            + ("This is a project run, so the file was named and placed "
               "automatically inside this diff's own output folder - no save "
               "location to pick by hand.\n\n" if auto_named else "")
            + "Every setting besides these four values (normal/search scale, search "
            "depth, registration error) is copied from a confirmed-working reference "
            "file. If you need to change something else (subsample radius, normal "
            "mode, etc.), generate a file via CloudCompare's own GUI instead, or "
            "share that value so the generator can be extended."
        )

    ttk.Button(dlg.form, text="Generate Params File...", command=generate_params).grid(
        row=dlg.row, column=0, columnspan=3, pady=(4, 8))
    dlg.row += 1

    dlg.add_file_field("params", "M3C2 params file (.txt):", [("Text files", "*.txt")])
    dlg.add_hint("Browse to a params file made via CloudCompare's GUI, or use "
                 "'Generate Params File...' above to write one from the values on this "
                 "page.")

    def build():
        active_pipeline = dlg.get_active_pipeline_for_run()
        baseline = dlg.require_existing_file("baseline", "Baseline .ply")
        comparison = dlg.require("comparison", "Comparison .ply")
        params = dlg.require("params", "M3C2 params file")
        registration_rms = dlg.fields["registration_rms"].get().strip() or None

        log_path = Path(comparison).with_name(Path(comparison).stem + "_m3c2_cc_log.txt")
        cmd = core.build_diff_command(baseline, comparison, params, log_file=log_path,
                                       pipeline=active_pipeline)

        # Watch the BASELINE's own folder, not the comparison's. CloudCompare's
        # -M3C2 treats the FIRST cloud passed to build_diff_command() as the
        # "compared"/core-points cloud - the only one that receives the M3C2
        # result and gets resaved by -SAVE_CLOUDS, into ITS OWN input folder.
        # build_diff_command() deliberately loads baseline first (see its own
        # docstring), so the new file appears next to baseline, not comparison.
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
                f"Baseline: {baseline}\n"
                f"Comparison: {comparison}\n"
                f"Params file used: {params}\n"
                + (f"Registration RMS on hand: {registration_rms} m. If you used "
                   f"'Generate Params File...' above, this is already baked into "
                   f"the params file. If you browsed to a file made via CloudCompare's "
                   f"GUI instead, make sure its registration-error was set to this "
                   f"value there.\n"
                   if registration_rms else
                   "No registration RMS was entered - if the params file's own "
                   "registration-error value is 0 or unset, the significance test "
                   "will over-flag almost everything as changed.\n")
                + "\n=== NOTE ===\n"
                "CloudCompare saves ALL loaded clouds, not just the M3C2 result "
                "- you'll see multiple output files. The one with the M3C2 "
                "distance scalar field (usually the sparser, more colorful "
                "cloud) is the actual result; the others are just copies of "
                "the input clouds.\n"
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

            summary += (
                "\n=== NEXT STEPS ===\n"
                "Open the result in CloudCompare, set the active scalar field "
                "to 'M3C2 distance', and check the color scale - red = "
                "positive change, blue = negative, magnitude = degree of change. "
                "Then use it as the input to Stage 6 (Classify)."
            )

            annotated = core.annotate_log_file(log_path, "Stage 5: Diff (M3C2)", cmd)
            if annotated:
                resolved_state["log_path"] = log_path
                summary += (
                    f"\n\n=== CLOUDCOMPARE LOG (with headers added) ===\n"
                    f"Saved to: {log_path}\n"
                    "Contains core point counts, timing, and any M3C2 warnings "
                    "(e.g. invalid normals) - open it directly for full detail."
                )
            else:
                summary += (
                    "\n\n=== CLOUDCOMPARE LOG ===\n"
                    "No log file was found at the expected path - check the "
                    "main window log instead."
                )
            return summary

        finish_info = None
        if active_pipeline is not None:
            finish_info = {"pipeline": active_pipeline, "stage_name": "diff",
                            "output": output_desired, "resolve_state": resolved_state}
        return cmd, build_report, finish_info

    dlg.add_run_button(build)
    return dlg


def open_classify_dialog(parent, run_callback, pipeline=None):
    dlg = StageDialog(parent, "Stage 6: Classify", run_callback)
    pipeline = pipeline if (pipeline is not None and pipeline.kind == "diff") else None
    dlg.pipeline = pipeline

    if pipeline is not None:
        dlg.add_pipeline_label(pipeline)

    default_script = str(Path(__file__).resolve().parent / "m3c2_classify.py")
    dlg.add_file_field("script", "Classify script (.py):", [("Python files", "*.py")],
                        default=default_script)
    dlg.add_file_field("input", "M3C2 diff result .ply:", [("PLY files", "*.ply")])
    if pipeline is not None:
        dlg.add_project_picker_button(
            "input", pipeline.project, lambda: pm.list_eligible_inputs(pipeline, "classify"))

    output_default = dlg.resolve_project_output_default(pipeline, "classify", ".ply")
    dlg.add_save_field("output", "Output .ply:", default_ext=".ply", default=output_default)

    dlg.add_file_field("comparison_for_rms", "Comparison .ply (from Stage 3, for RMS lookup):",
                        [("PLY files", "*.ply")])
    dlg.add_hint("Optional - for 'Load RMS' below. Must be the SAME comparison .ply used "
                 "as Stage 5's input (not the M3C2 result) - that's where Stage 3's RMS "
                 "sidecar is looked up from.")

    dlg.add_preset_selector("Threshold multiplier:", [
        ("2x RMS - more sensitive, more false positives from noise", {"rms_multiplier": "2.0"}),
        ("2.5x RMS - balanced (default)", {"rms_multiplier": "2.5"}),
        ("3x RMS - more conservative, may miss smaller real damage", {"rms_multiplier": "3.0"}),
    ])
    dlg.add_text_field("rms_multiplier", "Threshold multiplier:", default="2.5")

    dlg.add_text_field("threshold", "Distance threshold (m):", default="0.02")
    dlg.add_hint("Points with |M3C2 distance| below this are treated as noise and "
                 "dropped. Too low = false positives from noise; too high = real damage "
                 "filtered out. Start around 2-3x Stage 3's registration RMS - 'Load RMS' "
                 "below computes this automatically.")

    def load_rms_and_suggest():
        comparison_path = dlg.fields["comparison_for_rms"].get().strip()
        if not comparison_path:
            messagebox.showinfo("No comparison file",
                                 "Fill in the 'Comparison .ply (from Stage 3)' field first - "
                                 "the same file you used as Stage 5's comparison input.")
            return
        sidecar = core.load_rms_sidecar(comparison_path)
        if not (sidecar and sidecar.get("rms") is not None):
            messagebox.showinfo(
                "No RMS found",
                f"No saved RMS for:\n{comparison_path}\n\n"
                "This is only recorded when Stage 3 (Cleanup) ran with an "
                "'Align to baseline' target and successfully found an RMS "
                "value in the CloudCompare log."
            )
            return
        try:
            multiplier = float(dlg.fields["rms_multiplier"].get())
        except ValueError:
            messagebox.showerror("Invalid multiplier", "Threshold multiplier must be a number.")
            return

        rms = sidecar["rms"]
        suggested_threshold = rms * multiplier
        dlg.fields["threshold"].set(f"{suggested_threshold:.6f}")
        messagebox.showinfo(
            "Threshold suggested",
            f"Registration RMS: {rms:.6f} m\n"
            f"Multiplier: {multiplier}x\n"
            f"Suggested threshold: {suggested_threshold:.6f} m (filled in above)\n\n"
            "This is a starting point, not a guarantee - check the flagged percentage "
            "after running (0% or ~100% means it needs adjusting either direction)."
        )

    ttk.Button(dlg.form, text="Load RMS & Suggest Threshold", command=load_rms_and_suggest).grid(
        row=dlg.row, column=0, columnspan=3, pady=(4, 8))
    dlg.row += 1

    dlg.add_checkbox("keep_all", "Keep all points (add a flag field instead of filtering)")
    dlg.add_hint("Off by default - only flagged points are kept, for a clean "
                 "change-highlight cloud. Turn on to inspect the threshold's effect in "
                 "CloudCompare first.")

    dlg.add_checkbox("cluster", "Cluster flagged points into damage sites", default=True)
    dlg.add_hint("On by default. Groups flagged points by 3D position (DBSCAN/HDBSCAN) so "
                 "an isolated flagged point - likely noise, not real damage - gets "
                 "rejected as a second filter on top of the distance threshold. Each "
                 "surviving cluster gets a summary: centroid, point count, extent, "
                 "mean/max M3C2 magnitude.")

    dlg.add_radio_choice("cluster_method", "Clustering method:", [
        ("DBSCAN - fixed radius, validated on this sensor/environment already\n"
         "(same algorithm segment_planes.py uses)", "dbscan"),
        ("HDBSCAN - adapts to varying density automatically, worth trying if\n"
         "flagged-point density varies a lot by surface angle/distance from scanner", "hdbscan"),
    ], default="dbscan")

    dlg.add_text_field("cluster_eps", "Cluster gap tolerance (m, DBSCAN only):", default="0.05")
    dlg.add_hint("Max gap between flagged points to still count as one damage site. "
                 "Defaults tighter than Stage 4's clustering (0.15) since M3C2 core "
                 "points are usually finer-spaced - check point_spacing.py if sites "
                 "split/merge unexpectedly. Ignored for HDBSCAN.")

    dlg.add_text_field("cluster_min_samples", "Cluster density (min neighbors):", default="4")
    dlg.add_hint("How many flagged neighbors a point needs to seed a cluster at all - "
                 "DBSCAN/HDBSCAN's own density parameter.")

    dlg.add_text_field("min_cluster_size", "Minimum damage site size (points):", default="4")
    dlg.add_hint("An explicit size floor - e.g. 'need at least 4 flagged points to count "
                 "as a real site.' Smaller clusters fold into the rejected/noise count.")


    def build():
        script = dlg.require("script", "Classify script")
        input_ply = dlg.require("input", "M3C2 diff result .ply")
        output = dlg.require("output", "Output .ply")
        try:
            threshold = float(dlg.fields["threshold"].get())
        except ValueError:
            raise ValueError("Threshold must be a number, e.g. 0.02")
        keep_all = dlg.fields["keep_all"].get()
        cluster = dlg.fields["cluster"].get()
        cluster_method = dlg.fields["cluster_method"].get()
        try:
            cluster_eps = float(dlg.fields["cluster_eps"].get())
            cluster_min_samples = int(dlg.fields["cluster_min_samples"].get())
            min_cluster_size = int(dlg.fields["min_cluster_size"].get())
        except ValueError:
            raise ValueError("Cluster gap tolerance must be a number; cluster density and "
                              "minimum damage site size must be whole numbers.")

        active_pipeline = dlg.get_active_pipeline_for_run()
        cmd = core.build_classify_command(
            script, input_ply, output, threshold, keep_all,
            cluster=cluster, cluster_method=cluster_method, cluster_eps=cluster_eps,
            cluster_min_samples=cluster_min_samples, min_cluster_size=min_cluster_size,
            pipeline=active_pipeline)

        resolved_state = {}

        def build_report():
            summary = (
                "=== SUMMARY ===\n"
                f"Input: {input_ply}\n"
                f"Threshold: {threshold} m\n"
                f"Mode: {'keep all points, flag field added' if keep_all else 'filtered to flagged points only'}\n"
                f"Clustering: {'on (' + cluster_method + ')' if cluster else 'off'}\n"
                f"Saved to: {output}\n"
            )

            if cluster:
                extra_fields = core.resolve_classify_output(output)
                if not extra_fields:
                    summary += (
                        "\n=== OUTPUT ===\n"
                        "No *.clusters.json sidecar found next to the output file - the run "
                        "likely failed before reaching the clustering step, or produced no "
                        "flagged points at all. Check the main window log above.\n"
                    )
                else:
                    resolved_state["extra_fields"] = extra_fields
                    clusters = extra_fields.get("clusters", [])
                    summary += (
                        f"\nFlagged (Step A): {extra_fields.get('n_flagged', '?')}, "
                        f"confirmed damage sites (Step B/C): {extra_fields.get('n_confirmed', '?')}, "
                        f"rejected as spatial noise: {extra_fields.get('n_noise', '?')}\n"
                        f"Damage sites found: {len(clusters)}\n"
                    )
                    for c in clusters:
                        centroid = ", ".join(f"{v:.3f}" for v in c.get("centroid", []))
                        summary += (f"  site {c.get('cluster_id')}: {c.get('point_count')} points, "
                                    f"centroid=({centroid}), max|d|={c.get('max_magnitude', 0):.4f}\n")
                    if not clusters:
                        summary += ("No damage sites survived clustering - consider lowering "
                                    "the cluster density / minimum site size, or raising the "
                                    "cluster gap tolerance (DBSCAN), if real damage is being "
                                    "rejected as noise.\n")

            summary += (
                "\n=== NOTE ===\n"
                "Point counts and the flagged percentage are in the tool output "
                "below - check that a sensible fraction of points got flagged, "
                "not 0% (threshold too high) or nearly 100% (threshold too low).\n\n"
                "=== NEXT STEPS ===\n"
                + ("Open in CloudCompare to see which points got flagged before "
                   "deciding on a final threshold.\n"
                   if keep_all else
                   "Use this file as the input to Stage 7 (Surface), or directly as "
                   "the --change input to Stage 8 (Export) if reconstruction isn't needed.\n")
            )
            return summary

        finish_info = {"pipeline": active_pipeline, "stage_name": "classify",
                        "output": output, "resolve_state": resolved_state}
        return cmd, build_report, finish_info

    dlg.add_run_button(build)
    return dlg


def open_surface_dialog(parent, run_callback, pipeline=None):
    dlg = StageDialog(parent, "Stage 7: Surface", run_callback)
    pipeline = pipeline if (pipeline is not None and pipeline.kind == "diff") else None
    dlg.pipeline = pipeline

    if pipeline is not None:
        dlg.add_pipeline_label(pipeline)

    default_script = str(Path(__file__).resolve().parent / "surface_reconstruction.py")
    dlg.add_file_field("script", "Surface reconstruction script (.py):", [("Python files", "*.py")],
                        default=default_script)
    dlg.add_file_field("input", "Classified change cloud .ply:", [("PLY files", "*.ply")])
    if pipeline is not None:
        dlg.add_project_picker_button(
            "input", pipeline.project, lambda: pm.list_eligible_inputs(pipeline, "surface"))

    output_default = dlg.resolve_project_output_default(pipeline, "surface", ".ply")
    dlg.add_save_field("output", "Output mesh .ply:", default_ext=".ply", default=output_default)

    dlg.add_radio_choice("method", "Method:", [
        ("Poisson - smooth continuous surface, good for room shells/walls/floors", "poisson"),
        ("Ball Pivoting - stays closer to real points, better for cluttered/mechanical detail",
         "ball_pivoting"),
    ], hint=(
        "Poisson tends to over-smooth cluttered/complex scenes into blobs, since "
        "it fits one continuous surface through data that isn't actually one "
        "continuous surface. Ball Pivoting stays much closer to the actual point "
        "positions, at the cost of more holes where point density is uneven. "
        "Which suits a given change region is still an open question worth "
        "checking visually after each run."))

    dlg.add_text_field("depth", "[Poisson] Octree depth:", default="9")
    dlg.add_text_field("density_trim_percentile", "[Poisson] Density trim percentile:", default="10")
    dlg.add_hint("[Poisson only] Depth: higher = more detail but slower and more "
                 "artifact-prone; 8-10 is reasonable. Density trim: percentile of "
                 "lowest-density vertices removed as likely artifacts (0 disables "
                 "trimming, keeps the mesh watertight but keeps spurious blobs too).")
    dlg.add_text_field("ball_radii", "[Ball Pivoting] Ball radii (comma-separated, optional):", default="")
    dlg.add_hint("[Ball Pivoting only] e.g. '0.02,0.04,0.08', same units as the point "
                 "cloud. Leave blank to auto-estimate from the cloud's own point spacing.")
    dlg.add_text_field("carry_field", "Carry field (optional, e.g. 'M3C2 distance'):", default="M3C2 distance")
    dlg.add_hint("Carries this per-vertex scalar field onto the reconstructed mesh (via "
                 "nearest-original-point lookup), so it can still be colored by change "
                 "magnitude in USD. Requires scipy. Leave blank to skip.")


    def build():
        script = dlg.require("script", "Surface reconstruction script")
        input_ply = dlg.require("input", "Classified change cloud .ply")
        output = dlg.require("output", "Output mesh .ply")
        # Radio-button-backed now (add_radio_choice) - always exactly "poisson" or
        # "ball_pivoting", never blank/typo'd, so no fallback is needed here anymore.
        method = dlg.fields["method"].get()
        depth = None
        density_trim_percentile = None
        if method == "poisson":
            try:
                depth = int(dlg.fields["depth"].get())
                density_trim_percentile = float(dlg.fields["density_trim_percentile"].get())
            except ValueError:
                raise ValueError("Octree depth must be a whole number, density trim "
                                  "percentile must be a number.")
        ball_radii = dlg.fields["ball_radii"].get().strip() or None
        carry_field = dlg.fields["carry_field"].get().strip() or None

        active_pipeline = dlg.get_active_pipeline_for_run()
        cmd = core.build_surface_command(
            script, input_ply, output, method=method, depth=depth,
            density_trim_percentile=density_trim_percentile, ball_radii=ball_radii,
            carry_field=carry_field, pipeline=active_pipeline)
        finish_info = {"pipeline": active_pipeline, "stage_name": "surface", "output": output}

        report = (
            "=== SUMMARY ===\n"
            f"Input: {input_ply}\n"
            f"Method: {method}\n"
            + (f"Octree depth: {depth}, density trim: {density_trim_percentile}%\n"
               if method == "poisson" else
               f"Ball radii: {ball_radii or 'auto-estimated'}\n")
            + (f"Carrying field: {carry_field}\n" if carry_field else "No field carried through.\n")
            + f"Saved to: {output}\n\n"
            "=== NOTE ===\n"
            "Check the tool output below for vertex/triangle counts and whether the "
            "mesh came out watertight. 0 triangles means reconstruction likely failed "
            "- check the input cloud has enough points and reasonable density.\n\n"
            "=== NEXT STEPS ===\n"
            "Open the mesh in CloudCompare or Omniverse to check it visually, then use "
            "it as the --change input to Stage 8 (Export)."
        )
        return cmd, report, finish_info

    dlg.add_run_button(build)
    return dlg


def open_export_dialog(parent, run_callback, pipeline=None):
    dlg = StageDialog(parent, "Stage 8: Export to USD", run_callback)
    pipeline = pipeline if (pipeline is not None and pipeline.kind == "diff") else None
    dlg.pipeline = pipeline

    if pipeline is not None:
        dlg.add_pipeline_label(pipeline)

    default_script = str(Path(__file__).resolve().parent / "usd_export.py")
    dlg.add_file_field("script", "USD export script (.py):", [("Python files", "*.py")],
                        default=default_script)
    dlg.add_file_field("baseline", "Baseline .ply:", [("PLY files", "*.ply")])
    if pipeline is not None:
        dlg.add_project_picker_button(
            "baseline", pipeline.project, lambda: pm.list_side_candidates(pipeline.project, "baseline"))
    dlg.add_file_field("change", "Change-highlight .ply:", [("PLY files", "*.ply")])
    if pipeline is not None:
        dlg.add_project_picker_button(
            "change", pipeline.project, lambda: pm.list_eligible_inputs(pipeline, "export"))
    dlg.add_file_field("detail", "Damage detail .ply (optional):", [("PLY files", "*.ply")])
    dlg.add_hint("Optional. Output from extract_damage_detail.py - real comparison-cloud "
                 "geometry near flagged locations, shown alongside ChangeHighlight (which "
                 "is just a magnitude value at baseline positions). Leave blank to skip. "
                 "Not a tracked project stage - always picked by hand.")

    output_default = dlg.resolve_project_output_default(pipeline, "export", ".usd")
    dlg.add_save_field("output", "Output .usd:", default_ext=".usd", default=output_default)
    dlg.add_checkbox("package_usdz", "Also package as .usdz")
    dlg.add_hint("Most web, AR, and mobile USD viewers only accept .usdz, not raw .usd - "
                 "check this to preview without Omniverse. Saved next to the .usd file, "
                 "same name.")
    dlg.add_checkbox("downsample", "Downsample point cloud layers (reduces file size / viewer load)")
    dlg.add_text_field("voxel_size", "Voxel size:", default="")
    dlg.add_hint("Off by default. Keeps one point per voxel cell, removing "
                 "near-duplicate points from overlapping passes - stays safe as long as "
                 "voxel size is smaller than the smallest feature you care about. "
                 "Doesn't affect mesh layers. Check point_spacing.py first for a sense "
                 "of your data's spacing.")

    def build():
        script = dlg.require("script", "USD export script")
        active_pipeline = dlg.get_active_pipeline_for_run()
        baseline = dlg.require("baseline", "Baseline .ply")
        change = dlg.require("change", "Change-highlight .ply")
        detail = dlg.fields["detail"].get().strip() or None
        output = dlg.require("output", "Output .usd")
        package_usdz = dlg.fields["package_usdz"].get()
        voxel_size = None
        if dlg.fields["downsample"].get():
            try:
                voxel_size = float(dlg.fields["voxel_size"].get())
            except ValueError:
                raise ValueError("Voxel size must be a number when downsampling is checked.")
        cmd = core.build_export_command(script, baseline, change, output,
                                         package_usdz=package_usdz, detail_ply=detail,
                                         voxel_size=voxel_size, pipeline=active_pipeline)
        finish_info = {"pipeline": active_pipeline, "stage_name": "export", "output": output}

        report = (
            "=== SUMMARY ===\n"
            f"Baseline used: {baseline}\n"
            f"Change-highlight used: {change}\n"
            + (f"Damage detail used: {detail}\n" if detail else "No damage detail layer.\n")
            + (f"Downsampling: voxel size {voxel_size}\n" if voxel_size else "No downsampling.\n")
            + f"Script: {script}\n"
            f"Saved to: {output}\n"
            + (f"Also packaged as: {Path(output).with_suffix('.usdz')}\n" if package_usdz else "")
            + "\n=== NOTE ===\n"
            "The change input needs a real M3C2/distance field carried through (Stage 7 "
            "carries it by default). If the tool output below has a warning about a "
            "missing M3C2/distance field, re-check which file you pointed this at.\n\n"
            "=== NEXT STEPS ===\n"
            "Open the .usd file in Omniverse (or any USD-compatible viewer) "
            "to check the scene: /World/Compartment/Baseline should look "
            "like the environment in muted grey, and .../ChangeHighlight "
            "should show the change regions colored blue (negative) to "
            "red (positive) by magnitude"
            + (", and .../DamageDetail should show the real current geometry "
               "in the same colors. " if detail else ". ")
            + "Use the .usdz instead for "
            "web/AR/mobile viewers."
        )
        return cmd, report, finish_info

    dlg.add_run_button(build)
    return dlg


# ---------------------------------------------------------------------------
# About dialog
# ---------------------------------------------------------------------------

# About content now lives in about_content.json (see load_about_content above)
# so it can be edited without touching this file.


class AboutDialog(tk.Toplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.title("About: Pipeline Stages")
        self.geometry("560x520")
        self.minsize(400, 350)

        text = tk.Text(self, wrap="word", padx=12, pady=12, bg="#111", fg="#ddd")
        text.pack(fill="both", expand=True, side="left")
        scrollbar = ttk.Scrollbar(self, command=text.yview)
        scrollbar.pack(side="right", fill="y")
        text.config(yscrollcommand=scrollbar.set)

        text.tag_configure("heading", font=("Segoe UI", 12, "bold"), foreground="#fff",
                            spacing1=14, spacing3=4)
        text.tag_configure("subheading", font=("Segoe UI", 10, "bold"), foreground="#9cf",
                            spacing1=6)
        text.tag_configure("body", font=("Segoe UI", 10), spacing3=2)

        for stage_title, fields in load_about_content().items():
            text.insert("end", stage_title + "\n", "heading")
            for label, content in fields.items():
                text.insert("end", label + "\n", "subheading")
                text.insert("end", content + "\n", "body")

        text.config(state="disabled")

        ttk.Button(self, text="Close", command=self.destroy).pack(pady=8)


# ---------------------------------------------------------------------------
# Project / scan / diff dialogs
# ---------------------------------------------------------------------------

def _guess_source_type(path_str):
    """Maps a source file/folder to one of project_manager.VALID_SOURCE_TYPES
    ('pcap', 'osf', 'ros1_bag', 'ros2_bag'), auto-detecting from what's
    given - same "auto-detect from the file itself" philosophy already
    used by Stage 1's own backends. Returns None if it can't tell."""
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
    return None


class NewProjectDialog(tk.Toplevel):
    """Asks for a folder location, a compartment name, and a raw source
    file - then creates the project (and its baseline pipeline) and hands
    it back to the main window via on_created."""

    def __init__(self, parent, on_created):
        super().__init__(parent)
        self.title("New Project")
        self.resizable(True, False)
        self.minsize(420, 1)  # width floor only - height is already fixed
        self.on_created = on_created

        form = ttk.Frame(self, padding=12)
        form.grid(row=0, column=0, sticky="nsew")
        row = 0

        ttk.Label(form, text="Project location (parent folder):").grid(
            row=row, column=0, sticky="w", pady=3)
        self.location_var = tk.StringVar()
        ttk.Entry(form, textvariable=self.location_var, width=55).grid(
            row=row, column=1, padx=5)
        ttk.Button(form, text="Browse...", command=self._browse_location).grid(row=row, column=2)
        row += 1
        ttk.Label(form, text="The actual project folder is created here, named "
                              "automatically from the compartment name and today's date. "
                              "It will hold a baseline/ subfolder for this project's one "
                              "baseline pipeline.",
                  foreground="#777", font=("Segoe UI", 8), wraplength=380, justify="left"
                  ).grid(row=row, column=0, columnspan=3, sticky="w", pady=(0, 8))
        row += 1

        ttk.Label(form, text="Compartment name:").grid(row=row, column=0, sticky="w", pady=3)
        self.compartment_var = tk.StringVar()
        ttk.Entry(form, textvariable=self.compartment_var, width=55).grid(
            row=row, column=1, columnspan=2, padx=5, sticky="w")
        row += 1

        ttk.Label(form, text="Baseline raw source (pcap / OSF / .bag / ROS2 bag folder):").grid(
            row=row, column=0, sticky="w", pady=3)
        self.source_var = tk.StringVar()
        ttk.Entry(form, textvariable=self.source_var, width=55).grid(row=row, column=1, padx=5)
        btn_frame = ttk.Frame(form)
        btn_frame.grid(row=row, column=2)
        ttk.Button(btn_frame, text="File...", command=self._browse_source_file).pack(side="left")
        ttk.Button(btn_frame, text="Folder...", command=self._browse_source_folder).pack(side="left")
        row += 1
        ttk.Label(form, text="Source type (pcap/OSF/ROS1 bag/ROS2 bag) is auto-detected "
                              "from what you pick above.",
                  foreground="#777", font=("Segoe UI", 8), wraplength=380, justify="left"
                  ).grid(row=row, column=0, columnspan=3, sticky="w", pady=(0, 8))
        row += 1

        ttk.Button(form, text="Create Project", command=self._create).grid(
            row=row, column=0, columnspan=3, pady=(10, 0))

    def _browse_location(self):
        path = filedialog.askdirectory(parent=self)
        if path:
            self.location_var.set(path)

    def _browse_source_file(self):
        path = filedialog.askopenfilename(parent=self)
        if path:
            self.source_var.set(path)

    def _browse_source_folder(self):
        path = filedialog.askdirectory(parent=self)
        if path:
            self.source_var.set(path)

    def _create(self):
        location = self.location_var.get().strip()
        compartment = self.compartment_var.get().strip()
        source = self.source_var.get().strip()

        if not location or not compartment or not source:
            messagebox.showerror("Missing input", "Fill in all three fields.")
            return

        source_type = _guess_source_type(source)
        if source_type is None:
            messagebox.showerror(
                "Can't determine source type",
                f"Couldn't tell what kind of source this is from:\n{source}\n\n"
                "Expected a .pcap, .osf, or .bag file, or a folder containing a "
                "ROS2 bag (a metadata.yaml file alongside .db3 files)."
            )
            return

        try:
            project = pm.create_project(location, compartment, source, source_type)
        except pm.ProjectError as e:
            messagebox.showerror("Could not create project", str(e))
            return

        self.destroy()
        self.on_created(project)


class NewScanDialog(tk.Toplevel):
    """Asks for a label and a raw source file, then adds a new comparison
    scan to the currently open project (Section 10) and hands the new
    scan_id back via on_created."""

    def __init__(self, parent, project, on_created):
        super().__init__(parent)
        self.title("New Scan")
        self.resizable(True, False)
        self.minsize(420, 1)  # width floor only - height is already fixed
        self.project = project
        self.on_created = on_created

        form = ttk.Frame(self, padding=12)
        form.grid(row=0, column=0, sticky="nsew")
        row = 0

        ttk.Label(form, text=f"Project: {project.data.get('compartment', '?')}",
                  font=("Segoe UI", 9, "bold")).grid(row=row, column=0, columnspan=3, sticky="w", pady=(0, 8))
        row += 1

        ttk.Label(form, text="Label (e.g. 'post-storm', 'routine-check'):").grid(
            row=row, column=0, sticky="w", pady=3)
        self.label_var = tk.StringVar()
        ttk.Entry(form, textvariable=self.label_var, width=40).grid(
            row=row, column=1, columnspan=2, padx=5, sticky="w")
        row += 1
        ttk.Label(form, text="Today's date is added automatically, matching the format "
                              "already used for scan/diff IDs (e.g. 'post-storm_2026-09-01').",
                  foreground="#777", font=("Segoe UI", 8), wraplength=380, justify="left"
                  ).grid(row=row, column=0, columnspan=3, sticky="w", pady=(0, 8))
        row += 1

        ttk.Label(form, text="Raw source (pcap / OSF / .bag / ROS2 bag folder):").grid(
            row=row, column=0, sticky="w", pady=3)
        self.source_var = tk.StringVar()
        ttk.Entry(form, textvariable=self.source_var, width=40).grid(row=row, column=1, padx=5)
        btn_frame = ttk.Frame(form)
        btn_frame.grid(row=row, column=2)
        ttk.Button(btn_frame, text="File...", command=self._browse_source_file).pack(side="left")
        ttk.Button(btn_frame, text="Folder...", command=self._browse_source_folder).pack(side="left")
        row += 1

        ttk.Button(form, text="Add Scan", command=self._create).grid(
            row=row, column=0, columnspan=3, pady=(10, 0))

    def _browse_source_file(self):
        path = filedialog.askopenfilename(parent=self)
        if path:
            self.source_var.set(path)

    def _browse_source_folder(self):
        path = filedialog.askdirectory(parent=self)
        if path:
            self.source_var.set(path)

    def _create(self):
        label = self.label_var.get().strip()
        source = self.source_var.get().strip()
        if not label or not source:
            messagebox.showerror("Missing input", "Fill in both fields.")
            return

        source_type = _guess_source_type(source)
        if source_type is None:
            messagebox.showerror(
                "Can't determine source type",
                f"Couldn't tell what kind of source this is from:\n{source}\n\n"
                "Expected a .pcap, .osf, or .bag file, or a folder containing a "
                "ROS2 bag (a metadata.yaml file alongside .db3 files)."
            )
            return

        try:
            scan_id = pm.add_scan(self.project, label, source, source_type)
        except pm.ProjectError as e:
            messagebox.showerror("Could not add scan", str(e))
            return

        self.destroy()
        self.on_created(scan_id)


class NewDiffDialog(tk.Toplevel):
    """Asks for a label, a reference (the project's baseline or an
    existing scan), and a comparison (an existing scan), then adds a new
    diff pipeline to the currently open project (Section 11) and hands
    the new diff_id back via on_created."""

    def __init__(self, parent, project, on_created):
        super().__init__(parent)
        self.title("New Diff")
        self.resizable(True, False)
        self.minsize(420, 1)  # width floor only - height is already fixed
        self.project = project
        self.on_created = on_created

        form = ttk.Frame(self, padding=12)
        form.grid(row=0, column=0, sticky="nsew")
        row = 0

        ttk.Label(form, text=f"Project: {project.data.get('compartment', '?')}",
                  font=("Segoe UI", 9, "bold")).grid(row=row, column=0, columnspan=3, sticky="w", pady=(0, 8))
        row += 1

        scan_ids = pm.list_scans(project)
        if not scan_ids:
            ttk.Label(form, text="This project has no scans yet - add one with "
                                  "'New Scan' before creating a diff (a diff always "
                                  "needs at least one scan as its comparison side).",
                      foreground="#b00", wraplength=380, justify="left"
                      ).grid(row=row, column=0, columnspan=3, sticky="w", pady=(0, 8))
            row += 1

        ttk.Label(form, text="Label (e.g. 'post-storm_vs_baseline'):").grid(
            row=row, column=0, sticky="w", pady=3)
        self.label_var = tk.StringVar()
        ttk.Entry(form, textvariable=self.label_var, width=40).grid(
            row=row, column=1, columnspan=2, padx=5, sticky="w")
        row += 1

        ttk.Label(form, text="Reference (baseline side):").grid(row=row, column=0, sticky="w", pady=3)
        self.reference_var = tk.StringVar()
        reference_values = ["baseline"] + scan_ids
        ttk.Combobox(form, textvariable=self.reference_var, state="readonly",
                     width=38, values=reference_values).grid(row=row, column=1, columnspan=2, padx=5, sticky="w")
        row += 1
        ttk.Label(form, text="Either the project's own baseline, or an earlier scan - to "
                              "show what changed only since that scan, rather than since "
                              "the baseline.",
                  foreground="#777", font=("Segoe UI", 8), wraplength=380, justify="left"
                  ).grid(row=row, column=0, columnspan=3, sticky="w", pady=(0, 8))
        row += 1

        ttk.Label(form, text="Comparison (scan being checked):").grid(row=row, column=0, sticky="w", pady=3)
        self.comparison_var = tk.StringVar()
        ttk.Combobox(form, textvariable=self.comparison_var, state="readonly",
                     width=38, values=scan_ids).grid(row=row, column=1, columnspan=2, padx=5, sticky="w")
        row += 1

        ttk.Button(form, text="Add Diff", command=self._create).grid(
            row=row, column=0, columnspan=3, pady=(10, 0))

    def _create(self):
        label = self.label_var.get().strip()
        reference = self.reference_var.get().strip()
        comparison = self.comparison_var.get().strip()
        if not label or not reference or not comparison:
            messagebox.showerror("Missing input", "Fill in all three fields.")
            return

        try:
            diff_id = pm.add_diff(self.project, label, reference, comparison)
        except pm.ProjectError as e:
            messagebox.showerror("Could not add diff", str(e))
            return

        self.destroy()
        self.on_created(diff_id)


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class PipelineApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("SLAM Pipeline")
        self.geometry("760x580")
        self.minsize(700, 520)  # width matches what the Source/Diff pipeline
                                 # selector row above the buttons already
                                 # needs (two comboboxes + two labels + a
                                 # button, all on one row, unchanged here) -
                                 # height is taller than before since the
                                 # stage buttons now sit on two rows instead
                                 # of one wide row
        self.active_project = None
        self.active_source_pipeline = None  # PipelineHandle: baseline or a scan
        self.active_diff_pipeline = None    # PipelineHandle: a diff

        project_frame = ttk.Frame(self, padding=(12, 10, 12, 0))
        project_frame.pack(fill="x")
        ttk.Button(project_frame, text="New Project", command=self.new_project).pack(
            side="left", padx=(0, 4))
        ttk.Button(project_frame, text="Open Project", command=self.open_project).pack(
            side="left", padx=4)
        ttk.Button(project_frame, text="New Scan", command=self.new_scan).pack(
            side="left", padx=4)
        ttk.Button(project_frame, text="New Diff", command=self.new_diff).pack(
            side="left", padx=4)

        selector_frame = ttk.Frame(self, padding=(12, 6, 12, 0))
        selector_frame.pack(fill="x")
        ttk.Label(selector_frame, text="Source pipeline (Stages 1-3.5):").pack(side="left")
        self.source_pipeline_var = tk.StringVar()
        self.source_pipeline_combo = ttk.Combobox(
            selector_frame, textvariable=self.source_pipeline_var, state="readonly", width=28)
        self.source_pipeline_combo.pack(side="left", padx=(4, 8))
        self.source_pipeline_combo.bind("<<ComboboxSelected>>", self._on_source_pipeline_selected)
        ttk.Button(selector_frame, text="Set Decoded Source...",
                   command=self.set_decoded_source_for_active_pipeline).pack(
            side="left", padx=(0, 16))

        ttk.Label(selector_frame, text="Diff pipeline (Stages 4-7):").pack(side="left")
        self.diff_pipeline_var = tk.StringVar()
        self.diff_pipeline_combo = ttk.Combobox(
            selector_frame, textvariable=self.diff_pipeline_var, state="readonly", width=28)
        self.diff_pipeline_combo.pack(side="left", padx=(4, 0))
        self.diff_pipeline_combo.bind("<<ComboboxSelected>>", self._on_diff_pipeline_selected)

        status_frame = ttk.Frame(self, padding=(12, 4, 12, 0))
        status_frame.pack(fill="x")
        self.project_status_label = ttk.Label(status_frame, text="No active project",
                                                foreground="#555")
        self.project_status_label.pack(side="left")

        stage_row1 = ttk.Frame(self, padding=(12, 12, 12, 4))
        stage_row1.pack(fill="x")
        ttk.Button(stage_row1, text="1. Run SLAM",
                   command=lambda: open_slam_dialog(self, self.run_command, self.active_source_pipeline)
                   ).pack(side="left", padx=4)
        ttk.Button(stage_row1, text="2. Level",
                   command=lambda: open_level_dialog(self, self.run_command, self.active_source_pipeline)
                   ).pack(side="left", padx=4)
        ttk.Button(stage_row1, text="3. Run Cleanup",
                   command=lambda: open_cleanup_dialog(
                       self, self.run_command, self.active_source_pipeline, self.active_project)
                   ).pack(side="left", padx=4)
        ttk.Button(stage_row1, text="4. Segment",
                   command=lambda: open_segment_dialog(self, self.run_command, self.active_source_pipeline)
                   ).pack(side="left", padx=4)

        stage_row2 = ttk.Frame(self, padding=(12, 0, 12, 12))
        stage_row2.pack(fill="x")
        ttk.Button(stage_row2, text="5. Run Diff",
                   command=lambda: open_diff_dialog(self, self.run_command, self.active_diff_pipeline)
                   ).pack(side="left", padx=4)
        ttk.Button(stage_row2, text="6. Classify",
                   command=lambda: open_classify_dialog(self, self.run_command, self.active_diff_pipeline)
                   ).pack(side="left", padx=4)
        ttk.Button(stage_row2, text="7. Surface",
                   command=lambda: open_surface_dialog(self, self.run_command, self.active_diff_pipeline)
                   ).pack(side="left", padx=4)
        ttk.Button(stage_row2, text="8. Export to USD",
                   command=lambda: open_export_dialog(self, self.run_command, self.active_diff_pipeline)
                   ).pack(side="left", padx=4)
        ttk.Button(stage_row2, text="About",
                   command=lambda: AboutDialog(self)
                   ).pack(side="right", padx=4)

        ttk.Label(self, text="Log:", padding=(12, 0)).pack(anchor="w")

        log_frame = ttk.Frame(self, padding=(12, 0, 12, 12))
        log_frame.pack(fill="both", expand=True)
        self.log_text = tk.Text(log_frame, wrap="word", state="disabled", bg="#111", fg="#ddd")
        self.log_text.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        scrollbar.pack(side="right", fill="y")
        self.log_text.config(yscrollcommand=scrollbar.set)

    def new_project(self):
        if pm is None:
            messagebox.showerror("Not available", "project_manager.py could not be imported.")
            return
        NewProjectDialog(self, on_created=self._set_active_project)

    def open_project(self):
        if pm is None:
            messagebox.showerror("Not available", "project_manager.py could not be imported.")
            return
        folder = filedialog.askdirectory(parent=self, title="Select project folder")
        if not folder:
            return
        try:
            project = pm.load_project(folder)
        except pm.ProjectError as e:
            messagebox.showerror("Could not open project", str(e))
            return
        self._set_active_project(project)

    def new_scan(self):
        if self.active_project is None:
            messagebox.showerror("No active project", "Open or create a project first.")
            return
        NewScanDialog(self, self.active_project, on_created=self._on_scan_added)

    def new_diff(self):
        if self.active_project is None:
            messagebox.showerror("No active project", "Open or create a project first.")
            return
        NewDiffDialog(self, self.active_project, on_created=self._on_diff_added)

    def _set_active_project(self, project):
        self.active_project = project
        self.active_source_pipeline = project.baseline_handle()
        self.active_diff_pipeline = None
        self._refresh_pipeline_selectors()

    def _on_scan_added(self, scan_id):
        self.active_source_pipeline = self.active_project.scan_handle(scan_id)
        self._refresh_pipeline_selectors()

    def _on_diff_added(self, diff_id):
        self.active_diff_pipeline = self.active_project.diff_handle(diff_id)
        self._refresh_pipeline_selectors()

    def _refresh_pipeline_selectors(self):
        project = self.active_project
        if project is None:
            self.source_pipeline_combo["values"] = []
            self.diff_pipeline_combo["values"] = []
            self.source_pipeline_var.set("")
            self.diff_pipeline_var.set("")
            self.refresh_project_status()
            return

        source_values = ["Baseline"] + [f"Scan: {sid}" for sid in pm.list_scans(project)]
        self.source_pipeline_combo["values"] = source_values
        current_source_label = _pipeline_label(self.active_source_pipeline)
        self.source_pipeline_var.set(
            current_source_label if current_source_label in source_values else source_values[0])
        self._on_source_pipeline_selected()

        diff_values = [f"Diff: {did}" for did in pm.list_diffs(project)]
        self.diff_pipeline_combo["values"] = diff_values
        current_diff_label = _pipeline_label(self.active_diff_pipeline)
        if diff_values:
            self.diff_pipeline_var.set(
                current_diff_label if current_diff_label in diff_values else diff_values[0])
            self._on_diff_pipeline_selected()
        else:
            self.diff_pipeline_var.set("")
            self.active_diff_pipeline = None

        self.refresh_project_status()

    def _on_source_pipeline_selected(self, event=None):
        if self.active_project is None:
            return
        value = self.source_pipeline_var.get()
        if value == "Baseline":
            self.active_source_pipeline = self.active_project.baseline_handle()
        elif value.startswith("Scan: "):
            self.active_source_pipeline = self.active_project.scan_handle(value[len("Scan: "):])
        self.refresh_project_status()

    def _on_diff_pipeline_selected(self, event=None):
        if self.active_project is None:
            return
        value = self.diff_pipeline_var.get()
        if value.startswith("Diff: "):
            self.active_diff_pipeline = self.active_project.diff_handle(value[len("Diff: "):])
        self.refresh_project_status()

    def set_decoded_source_for_active_pipeline(self):
        """Points the active Source pipeline's Stage 1 (SLAM) input at an
        already-decoded bag that exists on disk, without needing to open
        the SLAM dialog and use its manual-override checkbox by hand.

        This is for a decoded bag that already exists - e.g. one produced
        by running decode_raw_packets.py directly, outside this applet's
        own auto-convert flow (which only offers to convert a folder that
        still LOOKS like it needs decoding - it has nothing to do, and
        nothing to offer, once a decoded bag already exists). Before this
        button existed, the only way to point a pipeline at an
        already-decoded bag was to open the SLAM dialog and check 'Use
        manual file selection instead' by hand, every time the dialog was
        reopened - reported as a real gap: the Source pipeline selector
        above lets you choose WHICH pipeline (baseline or a scan), but had
        no way to also choose which raw variant (original vs. decoded) of
        that pipeline's own input to use.

        Calls project_manager.set_decoded_raw_path() - the same call the
        SLAM dialog's own auto-convert flow makes - so this stays fully
        tracked in project.json (raw.decoded_path, PROJECT_SCHEMA_v2.md
        Section 12.1) and Stage 1 uses it automatically from here on. The
        original raw import (raw.path) is never touched by this - it
        keeps recording the true original source, same as always."""
        if pm is None:
            messagebox.showerror("Not available", "project_manager.py could not be imported.")
            return
        if self.active_project is None or self.active_source_pipeline is None:
            messagebox.showerror("No active source pipeline",
                                  "Open or create a project, and pick a Source pipeline above, "
                                  "first.")
            return

        folder = filedialog.askdirectory(
            parent=self,
            title="Select the decoded bag folder (contains .db3 + metadata.yaml)")
        if not folder:
            return

        pipeline_label = _pipeline_label(self.active_source_pipeline)
        proceed = messagebox.askyesno(
            "Set decoded source",
            f"Record:\n{folder}\n\n"
            f"as {pipeline_label}'s decoded SLAM input?\n\n"
            f"Stage 1 will use this automatically from now on (still tracked in "
            f"project.json). The original raw import stays recorded separately, "
            f"unchanged."
        )
        if not proceed:
            return

        try:
            pm.set_decoded_raw_path(self.active_source_pipeline, folder)
        except pm.ProjectError as e:
            messagebox.showerror("Could not set decoded source", str(e))
            return

        self.refresh_project_status()
        messagebox.showinfo(
            "Decoded source set",
            f"{pipeline_label} will use:\n{folder}\n\nfor Stage 1 (SLAM) from now on.")

    def refresh_project_status(self):
        if self.active_project is None:
            self.project_status_label.config(text="No active project")
            return
        compartment = self.active_project.data.get("compartment", "?")
        parts = [f"Project: {compartment}"]

        if self.active_source_pipeline is not None:
            next_stage = pm.find_next_stage(self.active_source_pipeline)
            parts.append(f"{_pipeline_label(self.active_source_pipeline)} next: {next_stage or 'done'}")
            raw = self.active_source_pipeline.entry.get("raw") or {}
            if raw.get("decoded_path"):
                parts.append("decoded source set")
        if self.active_diff_pipeline is not None:
            next_stage = pm.find_next_stage(self.active_diff_pipeline)
            parts.append(f"{_pipeline_label(self.active_diff_pipeline)} next: {next_stage or 'done'}")

        self.project_status_label.config(text="   |   ".join(parts))

    def log(self, message):
        self.log_text.config(state="normal")
        self.log_text.insert("end", message + "\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def run_command(self, cmd, on_complete=None):
        self.log(f"\n$ {' '.join(str(c) for c in cmd)}")

        def on_line(line):
            self.after(0, self.log, line)

        def on_done(returncode):
            status = "finished successfully" if returncode == 0 else f"exited with code {returncode}"
            self.after(0, self.log, f"[{status}]")
            if on_complete:
                self.after(0, on_complete, returncode)

        core.run_streaming(cmd, on_line, on_done)


if __name__ == "__main__":
    app = PipelineApp()
    app.mainloop()
