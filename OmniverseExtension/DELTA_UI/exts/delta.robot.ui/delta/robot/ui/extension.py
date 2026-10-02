"""DELTA USD damage inspection panel."""

import asyncio
import os

import omni.ext
import omni.kit.app
import omni.ui as ui
import omni.usd
from omni.kit.window.filepicker import FilePickerDialog
from omni.kit.window.title import get_main_window_title
from pxr import Gf, Usd, UsdGeom

from .damage_view import PointCallout, PointCameraNavigator


MAX_DAMAGE_POINTS = 5


class Extension(omni.ext.IExt):
    """DELTA extension entry point."""

    def on_startup(self, ext_id):
        """Extension startup."""
        print("[delta.robot.ui] Starting")
        title = get_main_window_title()
        if title:
            title.set_app_version("")

        self._damage_prim_path = None
        self._point_prim_paths = []
        self._damage_points = []
        self._total_damage_points = 0
        self._selected_damage_index = None
        self._callout = None
        self._camera_navigator = PointCameraNavigator()
        self._file_picker = None
        self._window = ui.Window(
            "DELTA",
            width=430,
            height=620,
        )

        self._window.frame.set_build_fn(self._build_ui)
        self._window.frame.rebuild()
        self._workspace_task = asyncio.ensure_future(self._configure_workspace())

    async def _configure_workspace(self):
        """Workspace layout."""
        for _ in range(10):
            await omni.kit.app.get_app().next_update_async()

        windows_to_hide = (
            "Console",
            "Render Settings",
            "Content",
            "Property",
            "Stage",
        )

        for window_title in windows_to_hide:
            kit_window = ui.Workspace.get_window(window_title)

            if kit_window:
                kit_window.visible = False

        viewport_window = ui.Workspace.get_window("Viewport")
        if self._window is None or viewport_window is None:
            return
        self._window.dock_in(viewport_window, ui.DockPosition.RIGHT, 0.24)

    def _find_point_prims(self, stage):
        """Points prim scan."""
        if stage is None:
            return []

        return sorted(
            str(prim.GetPath())
            for prim in stage.Traverse()
            if prim.IsA(UsdGeom.Points)
        )

    def _get_damage_prim(self, stage):
        """Damage prim validation."""
        if stage is None or not self._damage_prim_path:
            return None
        prim = stage.GetPrimAtPath(self._damage_prim_path)
        return prim if prim.IsValid() and prim.IsA(UsdGeom.Points) else None

    def _read_damage_points(self, stage):
        """Damage point preview."""
        self._total_damage_points = 0
        damage_prim = self._get_damage_prim(stage)
        if damage_prim is None:
            return []

        points = UsdGeom.Points(damage_prim).GetPointsAttr().Get()
        if points is None:
            return []

        self._total_damage_points = len(points)
        return list(points[:MAX_DAMAGE_POINTS])

    def _clear_damage_selection(self):
        """Damage selection reset."""
        self._selected_damage_index = None
        self._damage_points = []
        self._total_damage_points = 0
        if self._callout:
            self._callout.set_point(None)

    def _on_damage_prim_changed(self, model):
        """Damage prim selection."""
        index = model.as_int
        self._damage_prim_path = (
            self._point_prim_paths[index - 1]
            if 1 <= index <= len(self._point_prim_paths)
            else None
        )
        self._clear_damage_selection()
        asyncio.ensure_future(self._rebuild_ui_next_update())

    async def _rebuild_ui_next_update(self):
        """Deferred UI rebuild."""
        await omni.kit.app.get_app().next_update_async()
        if self._window:
            self._window.frame.rebuild()

    def _build_ui(self):
        """Panel layout."""
        stage = omni.usd.get_context().get_stage()
        self._point_prim_paths = self._find_point_prims(stage)
        if self._damage_prim_path not in self._point_prim_paths:
            self._damage_prim_path = None
            self._clear_damage_selection()
        selected_prim_index = (
            self._point_prim_paths.index(self._damage_prim_path) + 1
            if self._damage_prim_path else 0
        )
        self._damage_points = self._read_damage_points(stage)
        self._selected_damage_index = (
            min(self._selected_damage_index or 0, len(self._damage_points) - 1)
            if self._damage_points else None
        )

        damage_names = [
            f"Damage Point {index + 1}" for index in range(len(self._damage_points))
        ]
        if not damage_names:
            damage_names = ["No Damage Points Found"]

        if stage:
            stage_name = stage.GetRootLayer().GetDisplayName()
        else:
            stage_name = "No USD loaded"

        damage_prim_name = (
            self._damage_prim_path.rsplit("/", 1)[-1]
            if self._damage_prim_path else "None selected"
        )

        with ui.ScrollingFrame(
            height=ui.Fraction(1),
            horizontal_scrollbar_policy=ui.ScrollBarPolicy.SCROLLBAR_ALWAYS_OFF,
        ):
            with ui.VStack(height=0, spacing=8):
                ui.Label("DELTA", height=42, style={"font_size": 28})

                with ui.CollapsableFrame(
                    "Select USD Scene", collapsed=False, height=0
                ):
                    with ui.VStack(height=0, spacing=6):
                        ui.Label("Choose an inspection file", height=20)
                        ui.Button(
                            "SELECT USD FILE",
                            height=34,
                            clicked_fn=self._show_file_picker,
                        )

                with ui.CollapsableFrame(
                    "Damage Source Prim", collapsed=False, height=0
                ):
                    with ui.VStack(height=0, spacing=6):
                        ui.Label("Points prim containing damage", height=20)
                        self._prim_combo = ui.ComboBox(
                            selected_prim_index,
                            "Select a Points prim...",
                            *self._point_prim_paths,
                            height=28,
                        )
                        self._prim_combo.model.get_item_value_model().add_value_changed_fn(
                            self._on_damage_prim_changed
                        )
                        if not self._point_prim_paths:
                            ui.Label("No Points prims found", height=20)

                with ui.CollapsableFrame(
                    "Inspection Overview", collapsed=False, height=0
                ):
                    with ui.VStack(height=0, spacing=4):
                        ui.Label(
                            f"USD stage: {stage_name}", height=20, tooltip=stage_name
                        )
                        ui.Label(
                            f"Damage points: {self._total_damage_points:,}", height=20
                        )
                        ui.Label(
                            f"Source prim: {damage_prim_name}",
                            height=20,
                            tooltip=self._damage_prim_path or "",
                        )

                with ui.CollapsableFrame(
                    "Damage Points Selector", collapsed=False, height=0
                ):
                    with ui.VStack(height=0, spacing=6):
                        ui.Label(
                            f"Select a point (first {len(self._damage_points)} shown)"
                            if self._damage_points else "Choose a damage source above",
                            height=20,
                        )
                        self._damage_combo = ui.ComboBox(
                            self._selected_damage_index or 0,
                            *damage_names,
                            height=28,
                        )
                        self._details_label = ui.Label(
                            "No damage point data available", height=38
                        )
                        if self._selected_damage_index is not None:
                            self._show_point_details(self._selected_damage_index)
                        ui.Button(
                            "VIEW DAMAGE POINT",
                            height=34,
                            enabled=bool(self._damage_points),
                            clicked_fn=self._view_selected_damage,
                        )
                        self._damage_combo.model.get_item_value_model().add_value_changed_fn(
                            self._on_damage_point_changed
                        )

                ui.Button(
                    "REFRESH FROM USD", height=34, clicked_fn=self._refresh_ui
                )
                ui.Spacer(height=8)

    # File load function
    def _show_file_picker(self):
        if self._file_picker is None:
            self._file_picker = FilePickerDialog(
                "Select DELTA USD Scene",
                apply_button_label="Open USD",
                click_apply_handler=self._on_file_selected,
                click_cancel_handler=self._on_file_cancelled,
            )
        self._file_picker.show()

    def _on_file_selected(self, filename, dirname):
        file_path = os.path.join(dirname, filename)
        valid_extensions = (".usd", ".usda", ".usdc")

        if not file_path.lower().endswith(valid_extensions):
            print("[delta.robot.ui] Please select a USD file")
            return

        self._file_picker.hide()
        asyncio.ensure_future(self._open_usd(file_path))

    def _on_file_cancelled(self, filename, dirname):
        self._file_picker.hide()

    async def _open_usd(self, file_path):
        self._camera_navigator.reset_view()
        result, error_message = (
            await omni.usd.get_context().open_stage_async(file_path)
        )

        if result:
            print(f"[delta.robot.ui] Opened: {file_path}")
            self._damage_prim_path = None
            self._clear_damage_selection()
            if self._window:
                self._window.frame.rebuild()
        else:
            print(
                f"[delta.robot.ui] Could not open USD: "
                f"{error_message}"
            )

    def _on_damage_point_changed(self, model):
        """Damage point selection."""
        self._show_point_details(model.as_int)

    def _selected_world_point(self):
        """World position calculation."""
        if self._selected_damage_index is None or not self._damage_prim_path:
            return None
        stage = omni.usd.get_context().get_stage()
        if stage is None:
            return None
        prim = self._get_damage_prim(stage)
        if prim is None or self._selected_damage_index >= len(self._damage_points):
            return None
        local_point = Gf.Vec3d(*self._damage_points[self._selected_damage_index])
        world_matrix = UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(
            Usd.TimeCode.Default()
        )
        return world_matrix.Transform(local_point)

    def _show_point_details(self, selected_index):
        """Point details and callout."""
        if not 0 <= selected_index < len(self._damage_points):
            self._selected_damage_index = None
            self._details_label.text = "No damage point data available"
            if self._callout:
                self._callout.set_point(None)
            return

        self._selected_damage_index = selected_index
        world_point = self._selected_world_point()
        if world_point is None:
            self._details_label.text = "Selected point is unavailable"
            if self._callout:
                self._callout.set_point(None)
            return

        self._details_label.text = (
            f"Damage Point {selected_index + 1} (world position)\n"
            f"X: {world_point[0]:.3f}   "
            f"Y: {world_point[1]:.3f}   "
            f"Z: {world_point[2]:.3f}"
        )
        if self._callout is None:
            self._callout = PointCallout()
        self._callout.set_point(world_point)

    def _view_selected_damage(self):
        """Camera navigation."""
        world_point = self._selected_world_point()
        if world_point is None:
            return

        stage = omni.usd.get_context().get_stage()
        prim = self._get_damage_prim(stage)
        if prim is None:
            return
        extent = UsdGeom.Points(prim).GetExtentAttr().Get()
        diagonal = (
            (Gf.Vec3d(*extent[1]) - Gf.Vec3d(*extent[0])).GetLength()
            if extent and len(extent) == 2 else 0.0
        )
        distance = max(1.0, diagonal * 0.05)
        if not self._camera_navigator.move_to(stage, world_point, distance):
            print("[delta.robot.ui] No active viewport for damage navigation")

    def _refresh_ui(self):
        """Manual UI refresh."""
        if self._window:
            self._window.frame.rebuild()

    def on_shutdown(self):
        """Extension cleanup."""
        print("[delta.robot.ui] Shutting down")
        self._workspace_task.cancel()
        self._camera_navigator.reset_view()
        if self._callout:
            self._callout.destroy()
            self._callout = None

        if self._file_picker:
            self._file_picker.destroy()
            self._file_picker = None

        if self._window:
            self._window.destroy()
            self._window = None
