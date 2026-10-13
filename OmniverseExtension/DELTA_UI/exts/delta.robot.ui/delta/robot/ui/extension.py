"""DELTA Isaac Sim inspection panel."""

import asyncio
import os
from pathlib import Path

import omni.ext
import omni.kit.app
import omni.ui as ui
import omni.usd
from omni.kit.window.filepicker import FilePickerDialog
from omni.kit.window.title import get_main_window_title
from pxr import Gf, Usd, UsdGeom

from .damage_view import PointCallout, PointCameraNavigator
from .usd_builder_window import UsdBuilderWindow


MAX_DAMAGE_POINTS = 100
DAMAGE_TOKENS = ("damage", "defect", "change", "highlight", "loss")


class Extension(omni.ext.IExt):
    """DELTA extension entry point."""

    # ---------- Startup and workspace ----------

    def on_startup(self, _ext_id):
        print("[delta.robot.ui] Starting version 05")
        title = get_main_window_title()
        if title:
            title.set_app_version("")

        self._all_prim_paths = []
        self._point_prim_paths = []
        self._damage_prim_path = None
        self._damage_points = []
        self._damage_values = []
        self._total_damage_points = 0
        self._selected_damage_index = None

        self._file_picker = None
        self._builder_window = None
        self._callout = None
        self._camera_navigator = PointCameraNavigator()
        self._window = ui.Window("DELTA", width=450, height=760)
        self._window.frame.set_build_fn(self._build_ui)
        self._window.frame.rebuild()

        self._stage_subscription = (
            omni.usd.get_context()
            .get_stage_event_stream()
            .create_subscription_to_pop(
                self._on_stage_event,
                name="DeltaStageUpdates",
            )
        )
        self._workspace_task = asyncio.ensure_future(self._configure_workspace())

    async def _configure_workspace(self):
        for _ in range(10):
            await omni.kit.app.get_app().next_update_async()

        for window_title in ("Console", "Render Settings", "Content", "Property"):
            window = ui.Workspace.get_window(window_title)
            if window:
                window.visible = False

        viewport = ui.Workspace.get_window("Viewport")
        if self._window and viewport:
            self._window.dock_in(viewport, ui.DockPosition.RIGHT, 0.25)

    # ---------- Scene and prim discovery ----------

    def _scan_stage(self, stage):
        if stage is None:
            return [], []

        all_prims = []
        point_prims = []
        for prim in stage.Traverse():
            if not prim.IsActive() or not prim.IsDefined():
                continue
            path = str(prim.GetPath())
            all_prims.append(path)
            if prim.IsA(UsdGeom.Points):
                point_prims.append(path)
        return sorted(all_prims), sorted(point_prims)

    def _auto_damage_source(self, stage):
        if not self._point_prim_paths:
            return None

        def score(path):
            prim = stage.GetPrimAtPath(path)
            lowered = path.lower()
            value = 0
            if prim.GetAttribute("primvars:deltaSignedDistance").IsValid():
                value += 1000
            if lowered.endswith("/damagedetail"):
                value += 500
            for index, token in enumerate(DAMAGE_TOKENS):
                if token in lowered:
                    value += 100 - index
            return value

        best_path = max(self._point_prim_paths, key=score)
        return best_path if score(best_path) > 0 else None

    def _get_damage_prim(self, stage):
        if stage is None or not self._damage_prim_path:
            return None
        prim = stage.GetPrimAtPath(self._damage_prim_path)
        return prim if prim.IsValid() and prim.IsA(UsdGeom.Points) else None

    def _read_damage_points(self, stage):
        prim = self._get_damage_prim(stage)
        if prim is None:
            self._total_damage_points = 0
            self._damage_values = []
            return []

        points = UsdGeom.Points(prim).GetPointsAttr().Get()
        values = prim.GetAttribute("primvars:deltaSignedDistance").Get()
        points = points if points is not None else []
        values = values if values is not None else []
        self._total_damage_points = len(points)
        self._damage_values = list(values[:MAX_DAMAGE_POINTS])
        return list(points[:MAX_DAMAGE_POINTS])

    # ---------- Main interface ----------

    def _build_ui(self):
        stage = omni.usd.get_context().get_stage()
        self._all_prim_paths, self._point_prim_paths = self._scan_stage(stage)

        if self._damage_prim_path not in self._point_prim_paths:
            self._damage_prim_path = self._auto_damage_source(stage)
            self._selected_damage_index = 0 if self._damage_prim_path else None

        self._damage_points = self._read_damage_points(stage)
        if not self._damage_points:
            self._selected_damage_index = None
            if self._callout:
                self._callout.set_point(None)
        elif self._selected_damage_index is None:
            self._selected_damage_index = 0
        else:
            self._selected_damage_index = min(
                self._selected_damage_index,
                len(self._damage_points) - 1,
            )

        stage_name = stage.GetRootLayer().GetDisplayName() if stage else "No USD loaded"
        source_index = (
            self._point_prim_paths.index(self._damage_prim_path) + 1
            if self._damage_prim_path in self._point_prim_paths
            else 0
        )

        with ui.ScrollingFrame(
            height=ui.Fraction(1),
            horizontal_scrollbar_policy=ui.ScrollBarPolicy.SCROLLBAR_ALWAYS_OFF,
        ):
            with ui.VStack(height=0, spacing=8):
                ui.Label("DELTA", height=42, style={"font_size": 28})

                with ui.CollapsableFrame("USD Scene", collapsed=False, height=0):
                    with ui.VStack(height=0, spacing=6):
                        ui.Label(f"Current: {stage_name}", height=20, tooltip=stage_name)
                        # BUTTON: Opens the USD file picker. Edit the next quoted string to rename this button.
                        ui.Button(
                            "SELECT USD FILE",
                            height=36,
                            clicked_fn=self._show_file_picker,
                        )

                with ui.CollapsableFrame("Prim Navigator", collapsed=False, height=0):
                    with ui.VStack(height=0, spacing=6):
                        prim_names = self._all_prim_paths or ["No prims available"]
                        self._prim_combo = ui.ComboBox(0, *prim_names, height=28)
                        self._prim_combo_model = (
                            self._prim_combo.model.get_item_value_model()
                        )
                        with ui.HStack(height=34, spacing=6):
                            # BUTTON: Selects the prim chosen above in the scene. The first string is the button text.
                            ui.Button("SELECT PRIM", clicked_fn=self._select_prim)
                            # BUTTON: Clears the current prim selection. Edit "CLEAR" to change its caption.
                            ui.Button("CLEAR", width=90, clicked_fn=self._clear_prim)

                with ui.CollapsableFrame(
                    "Automatic Damage Detection", collapsed=False, height=0
                ):
                    with ui.VStack(height=0, spacing=6):
                        self._damage_source_combo = ui.ComboBox(
                            source_index,
                            "No Points prim selected",
                            *self._point_prim_paths,
                            height=28,
                        )
                        self._damage_source_combo.model.get_item_value_model().add_value_changed_fn(
                            self._on_damage_source_changed
                        )
                        ui.Label(
                            f"Detected points: {self._total_damage_points:,}",
                            height=20,
                        )
                        ui.Label(
                            f"Source: {self._damage_prim_path or 'None'}",
                            height=20,
                            tooltip=self._damage_prim_path or "",
                        )

                with ui.CollapsableFrame("Damage Point", collapsed=False, height=0):
                    with ui.VStack(height=0, spacing=6):
                        names = [
                            self._damage_point_name(index)
                            for index in range(len(self._damage_points))
                        ] or ["No damage points found"]
                        self._damage_combo = ui.ComboBox(
                            self._selected_damage_index or 0,
                            *names,
                            height=28,
                        )
                        self._details_label = ui.Label(
                            "No damage point data available",
                            height=54,
                        )
                        if self._selected_damage_index is not None:
                            self._show_damage_details(self._selected_damage_index)
                        self._damage_combo.model.get_item_value_model().add_value_changed_fn(
                            self._on_damage_point_changed
                        )
                        # BUTTON: Marks the chosen damage point and moves the camera to it. Caption is below.
                        ui.Button(
                            "VIEW DAMAGE POINT",
                            height=36,
                            enabled=bool(self._damage_points),
                            clicked_fn=self._view_selected_damage,
                        )

                with ui.CollapsableFrame("USD Creation", collapsed=False, height=0):
                    with ui.VStack(height=0, spacing=6):
                        ui.Label(
                            "Create a baseline or damage-comparison USD from PLY files.",
                            word_wrap=True,
                            height=34,
                        )
                        # BUTTON: Opens the PLY processing window. Caption is below; clicked_fn names the action.
                        ui.Button(
                            "OPEN USD BUILDER",
                            height=38,
                            clicked_fn=self._show_builder,
                        )

                with ui.CollapsableFrame("Colour Key", collapsed=True, height=0):
                    with ui.VStack(height=0, spacing=5):
                        ui.Label("Grey - baseline")
                        ui.Label("Red - material loss")
                        ui.Label("Cyan - new material")

                # BUTTON: Rebuilds the panel from the current scene. Edit "REFRESH" to rename it.
                ui.Button("REFRESH", height=34, clicked_fn=self._refresh_ui)
                ui.Spacer(height=8)

    # ---------- USD loading ----------

    @staticmethod
    def _filter_usd_item(item):
        if not item or item.is_folder:
            return True
        return Path(item.path).suffix.lower() in {".usd", ".usda", ".usdc"}

    def _show_file_picker(self):
        if self._file_picker is None:
            self._file_picker = FilePickerDialog(
                "Select DELTA USD Scene",
                apply_button_label="Open USD",
                click_apply_handler=self._on_file_selected,
                click_cancel_handler=self._on_file_cancelled,
                item_filter_options=["USD files (*.usd, *.usda, *.usdc)"],
                item_filter_fn=self._filter_usd_item,
            )
        self._file_picker.show()

    def _on_file_selected(self, filename, dirname):
        path = os.path.join(dirname, filename)
        if not path.lower().endswith((".usd", ".usda", ".usdc")):
            print("[delta.robot.ui] Select a USD, USDA, or USDC file")
            return
        self._file_picker.hide()
        asyncio.ensure_future(self._open_usd(path))

    def _on_file_cancelled(self, _filename, _dirname):
        self._file_picker.hide()

    async def _open_usd(self, path):
        self._camera_navigator.reset_view()
        result, error = await omni.usd.get_context().open_stage_async(path)
        if not result:
            print(f"[delta.robot.ui] Could not open USD: {error}")
            return
        self._damage_prim_path = None
        self._selected_damage_index = None
        self._refresh_ui()

    # ---------- Prim navigation ----------

    def _select_prim(self):
        if not self._all_prim_paths:
            return
        index = max(0, min(self._prim_combo_model.as_int, len(self._all_prim_paths) - 1))
        omni.usd.get_context().get_selection().set_selected_prim_paths(
            [self._all_prim_paths[index]],
            True,
        )

    def _clear_prim(self):
        omni.usd.get_context().get_selection().set_selected_prim_paths([], True)

    # ---------- Damage inspection ----------

    def _on_damage_source_changed(self, model):
        index = model.as_int
        self._damage_prim_path = (
            self._point_prim_paths[index - 1]
            if 1 <= index <= len(self._point_prim_paths)
            else None
        )
        self._selected_damage_index = 0 if self._damage_prim_path else None
        asyncio.ensure_future(self._refresh_next_update())

    def _damage_point_name(self, index):
        if index >= len(self._damage_values):
            return f"Damage Point {index + 1}"
        value = float(self._damage_values[index])
        kind = "Material Loss" if value < 0 else "New Material"
        return f"{kind} {index + 1}"

    def _on_damage_point_changed(self, model):
        self._show_damage_details(model.as_int)

    def _selected_world_point(self):
        if self._selected_damage_index is None:
            return None
        stage = omni.usd.get_context().get_stage()
        prim = self._get_damage_prim(stage)
        if prim is None or self._selected_damage_index >= len(self._damage_points):
            return None

        local = Gf.Vec3d(*self._damage_points[self._selected_damage_index])
        transform = UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(
            Usd.TimeCode.Default()
        )
        return transform.Transform(local)

    def _show_damage_details(self, index):
        if not 0 <= index < len(self._damage_points):
            self._selected_damage_index = None
            self._details_label.text = "No damage point data available"
            if self._callout:
                self._callout.set_point(None)
            return

        self._selected_damage_index = index
        world = self._selected_world_point()
        if world is None:
            return

        detail = self._damage_point_name(index)
        self._details_label.text = (
            f"{detail}\n"
            f"X: {world[0]:.3f}   Y: {world[1]:.3f}   Z: {world[2]:.3f}"
        )
        if self._callout is None:
            self._callout = PointCallout()
        self._callout.set_point(world)

    def _view_selected_damage(self):
        world = self._selected_world_point()
        stage = omni.usd.get_context().get_stage()
        prim = self._get_damage_prim(stage)
        if world is None or prim is None:
            return

        extent = UsdGeom.Points(prim).GetExtentAttr().Get()
        diagonal = (
            (Gf.Vec3d(*extent[1]) - Gf.Vec3d(*extent[0])).GetLength()
            if extent and len(extent) == 2
            else 0.0
        )
        self._camera_navigator.move_to(stage, world, max(1.0, diagonal * 0.05))

    # ---------- Refresh and builder ----------

    def _on_stage_event(self, event):
        if event.type in (
            int(omni.usd.StageEventType.OPENED),
            int(omni.usd.StageEventType.CLOSED),
        ):
            asyncio.ensure_future(self._refresh_next_update())

    async def _refresh_next_update(self):
        await omni.kit.app.get_app().next_update_async()
        self._refresh_ui()

    def _refresh_ui(self):
        if self._window:
            self._window.frame.rebuild()

    def _show_builder(self):
        if self._builder_window is None:
            self._builder_window = UsdBuilderWindow(on_usd_opened=self._refresh_ui)
        else:
            self._builder_window.show()

    # ---------- Shutdown ----------

    def on_shutdown(self):
        print("[delta.robot.ui] Shutting down version 05")
        if self._workspace_task:
            self._workspace_task.cancel()
        self._stage_subscription = None
        self._camera_navigator.reset_view()

        if self._callout:
            self._callout.destroy()
            self._callout = None
        if self._builder_window:
            self._builder_window.destroy()
            self._builder_window = None
        if self._file_picker:
            self._file_picker.destroy()
            self._file_picker = None
        if self._window:
            self._window.destroy()
            self._window = None
