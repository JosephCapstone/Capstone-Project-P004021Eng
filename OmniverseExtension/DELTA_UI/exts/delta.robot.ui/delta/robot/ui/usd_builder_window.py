"""PLY-to-USD builder window."""

import asyncio
import queue
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import omni.kit.app
import omni.ui as ui
import omni.usd
from omni.kit.window.filepicker import FilePickerDialog

from .processing_worker import QUALITY_PRESETS, build_usd


class UsdBuilderWindow:
    """Creates baseline or damage-comparison USD scenes from PLY files."""

    # ---------- Window lifecycle ----------

    def __init__(self, on_usd_opened=None):
        self._on_usd_opened = on_usd_opened
        self._window = None
        self._file_picker = None
        self._future = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="delta-usd")
        self._messages = queue.Queue()
        self._log_lines = []

        self._baseline_model = ui.SimpleStringModel("")
        self._inspection_model = ui.SimpleStringModel("")
        self._output_model = ui.SimpleStringModel(
            str(Path.home() / "Documents" / "UNI" / "DELTA" / "Data" / "Processed")
        )
        self._quality_model = None
        self._status_label = None
        self._log_label = None

        self._subscription = (
            omni.kit.app.get_app()
            .get_update_event_stream()
            .create_subscription_to_pop(self._on_update, name="DeltaUsdBuilder")
        )
        self.show()

    def show(self):
        if self._window is None:
            self._window = ui.Window("DELTA USD Builder", width=720, height=650)
            self._window.frame.set_build_fn(self._build_ui)
        self._window.visible = True
        self._window.frame.rebuild()

    # ---------- Interface layout ----------

    def _build_ui(self):
        with ui.VStack(spacing=9):
            ui.Label("Create USD from PLY", style={"font_size": 26})
            ui.Label(
                "Create a new reference USD from one PLY, or compare an inspection "
                "PLY against an existing baseline PLY.",
                word_wrap=True,
                height=44,
            )

            # Show baseline and inspection file inputs.
            with ui.CollapsableFrame("1. Select point clouds", collapsed=False):
                with ui.VStack(spacing=6):
                    ui.Label("Baseline PLY")
                    with ui.HStack(height=30, spacing=6):
                        ui.StringField(model=self._baseline_model)
                        ui.Button("Browse", width=90, clicked_fn=self._browse_baseline)

                    ui.Label("Inspection PLY - required only for comparison")
                    with ui.HStack(height=30, spacing=6):
                        ui.StringField(model=self._inspection_model)
                        ui.Button("Browse", width=90, clicked_fn=self._browse_inspection)
                        ui.Button("Clear", width=70, clicked_fn=self._clear_inspection)

            # Show the processing quality options.
            with ui.CollapsableFrame("2. Processing quality", collapsed=False):
                self._quality_combo = ui.ComboBox(1, *QUALITY_PRESETS.keys())
                self._quality_model = self._quality_combo.model.get_item_value_model()

            # Show the output folder selector.
            with ui.CollapsableFrame("3. Output folder", collapsed=False):
                with ui.HStack(height=30, spacing=6):
                    ui.StringField(model=self._output_model)
                    ui.Button("Browse", width=90, clicked_fn=self._browse_output)

            # Show the baseline and comparison processing controls.
            with ui.HStack(height=48, spacing=8):
                ui.Button(
                    "CREATE NEW BASELINE USD",
                    clicked_fn=self._start_new_baseline,
                )
                ui.Button(
                    "CREATE DAMAGE COMPARISON USD",
                    clicked_fn=self._start_comparison,
                )
            self._status_label = ui.Label("Ready", height=24, style={"font_size": 18})

            with ui.ScrollingFrame(height=260):
                self._log_label = ui.Label(
                    "Baseline only creates a reference USD. Adding an inspection "
                    "creates a damage-comparison USD.",
                    word_wrap=True,
                    alignment=ui.Alignment.LEFT_TOP,
                )

    # ---------- File selection ----------

    def _browse(self, title, model, directory_only=False, suffixes=None):
        picker = None

        def filter_item(item):
            if not item or item.is_folder:
                return True
            if directory_only:
                return False
            return Path(item.path).suffix.lower() in suffixes

        def apply(filename, dirname):
            model.set_value(dirname if directory_only else str(Path(dirname) / filename))
            picker.hide()

        def cancel(_filename, _dirname):
            picker.hide()

        picker = FilePickerDialog(
            title,
            apply_button_label="Select",
            click_apply_handler=apply,
            click_cancel_handler=cancel,
            item_filter_options=["PLY files (*.ply)"] if suffixes else ["Folders"],
            item_filter_fn=filter_item,
        )
        self._file_picker = picker
        picker.show()

    def _browse_baseline(self):
        self._browse("Select baseline PLY", self._baseline_model, suffixes={".ply"})

    def _browse_inspection(self):
        self._browse("Select inspection PLY", self._inspection_model, suffixes={".ply"})

    def _browse_output(self):
        self._browse("Select output folder", self._output_model, directory_only=True)

    def _clear_inspection(self):
        self._inspection_model.set_value("")

    # ---------- Processing workflow ----------

    def _selected_quality(self):
        index = self._quality_model.as_int if self._quality_model else 1
        names = list(QUALITY_PRESETS)
        return names[max(0, min(index, len(names) - 1))]

    def _start_new_baseline(self):
        self._start(include_inspection=False)

    def _start_comparison(self):
        self._start(include_inspection=True)

    def _start(self, include_inspection):
        if self._future and not self._future.done():
            self._set_status("Processing is already running")
            return

        baseline = Path(self._baseline_model.as_string.strip())
        inspection_text = self._inspection_model.as_string.strip()
        inspection = Path(inspection_text) if include_inspection and inspection_text else None
        output_text = self._output_model.as_string.strip()
        output_folder = Path(output_text) if output_text else None

        # Check the input files before starting processing.
        if not baseline.is_file() or baseline.suffix.lower() != ".ply":
            self._set_status("Select an existing baseline PLY")
            return
        if include_inspection and inspection is None:
            self._set_status("Select an inspection PLY for comparison")
            return
        if inspection and (not inspection.is_file() or inspection.suffix.lower() != ".ply"):
            self._set_status("Select an existing inspection PLY")
            return
        if output_folder is None:
            self._set_status("Select an output folder")
            return

        output_folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        name = "delta_inspection" if inspection else "delta_baseline"
        output = output_folder / f"{name}_{stamp}.usd"

        self._log_lines = []
        self._set_status("Processing")
        # Start processing in the background.
        self._future = self._executor.submit(
            build_usd,
            baseline,
            inspection,
            output,
            self._selected_quality(),
            self._messages.put,
        )

    # Display processing messages and handle the finished result.
    def _on_update(self, _event):
        while True:
            try:
                self._append_log(self._messages.get_nowait())
            except queue.Empty:
                break

        if not self._future or not self._future.done():
            return

        future = self._future
        self._future = None
        try:
            result = future.result()
        except Exception as exc:
            self._append_log(f"ERROR: {exc}")
            self._set_status("Failed")
            return

        self._set_status(f"Complete - {result['damage_points']:,} damage points")
        asyncio.ensure_future(self._open_usd(result["output"]))

    # Open the generated USD in Isaac Sim.
    async def _open_usd(self, path):
        result, error = await omni.usd.get_context().open_stage_async(path)
        if not result:
            self._append_log(f"Could not open USD: {error}")
            return
        if self._on_usd_opened:
            self._on_usd_opened()

    # ---------- Status and cleanup ----------

    def _set_status(self, text):
        if self._status_label:
            self._status_label.text = text

    def _append_log(self, text):
        self._log_lines.append(str(text))
        self._log_lines = self._log_lines[-100:]
        if self._log_label:
            self._log_label.text = "\n".join(self._log_lines)

    def destroy(self):
        self._subscription = None
        if self._file_picker:
            self._file_picker.destroy()
            self._file_picker = None
        if self._window:
            self._window.destroy()
            self._window = None
        self._executor.shutdown(wait=False, cancel_futures=True)
