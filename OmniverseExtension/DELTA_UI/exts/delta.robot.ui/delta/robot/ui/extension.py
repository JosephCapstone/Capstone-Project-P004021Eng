"""DELTA: damage-site inspection and Callum's original processing app."""
import asyncio
import os
import carb.settings
from pathlib import Path
import omni.ext
import omni.kit.app
import omni.ui as ui
import omni.usd
from omni.kit.window.filepicker import FilePickerDialog
from .damage_view import PointCameraNavigator, ClusterBoxCallout
from .cluster_model import read_clusters, scene_layers, SceneVisibility, InspectionSession, ClusterHighlight
from .pipeline_launcher import launch_pipeline, pipeline_status
from .section_plane import SectionPlane
from .damage_summary import site_summary


class Extension(omni.ext.IExt):
    def on_startup(self, _ext_id):
        self._clusters = []
        self._site_names = {}
        self._index = None
        self._stage = None
        self._slicer = None
        self._slice_render_settings = None
        self._slice_pending = False
        self._slice_checkbox_sync = False
        self._slice_position_label = None
        self._visibility = None
        self._inspection_session = None
        self._settings = carb.settings.get_settings()
        for loop in ("main", "present", "rendering_0"):
            self._settings.set_bool(f"/app/runLoops/{loop}/rateLimitEnabled", True)
            self._settings.set_int(f"/app/runLoops/{loop}/rateLimitFrequency", 30)
        self._settings.set_bool("/app/runLoops/main/rateLimitUseBusyLoop", False)
        self._settings.set_float_array("/rtx/sceneDb/ambientLightColor", [1.0, 1.0, 1.0])
        self._settings.set_float("/rtx/sceneDb/ambientLightIntensity", .08)
        self._settings.set("/rtx/rendermode", "MinimalRendering")
        self._settings.set("/exts/omni.kit.viewport.menubar.camera/visible", False)
        self._settings.set("/exts/omni.kit.viewport.menubar.camera/expand", False)
        self._settings.set("/persistent/exts/omni.kit.viewport.menubar.camera/visible", False)
        self._settings.set("/persistent/exts/omni.kit.viewport.menubar.camera/expand", False)
        self._original_operation = self._settings.get("/app/transform/operation")
        self._original_tools = self._settings.get("/exts/omni.kit.manipulator.prim.core/tools/enabled")
        self._settings.set("/app/transform/operation", "select")
        self._settings.set("/exts/omni.kit.manipulator.prim.core/tools/enabled", False)
        self._operation_sub = self._settings.subscribe_to_node_change_events("/app/transform/operation", self._keep_select)
        self._layers = {}
        self._enabled = {}
        self._isolated = False
        self._highlight = None
        self._box_callout = ClusterBoxCallout(self._show_configuration)
        self._camera = PointCameraNavigator()
        self._distance = None
        self._picker = None
        self._pipeline_process = None
        self._message = "Ready"
        self._config_window = None
        self._all_prim_paths = []
        self._point_prim_paths = []
        self._damage_source_path = None
        self._source_info = None
        self._changing_selection = False
        self._view = "Free"
        self._window = ui.Window("DELTA", width=490, height=820)
        self._window.frame.set_build_fn(self._build_ui)
        self._subscription = omni.usd.get_context().get_stage_event_stream().create_subscription_to_pop(
            self._on_stage_event, name="DeltaClusterSelection")
        self._slice_subscription = omni.kit.app.get_app().get_update_event_stream().create_subscription_to_pop(
            self._update_slice_viewport, name="DeltaSliceViewport")
        self._refresh()
        self._workspace_task = asyncio.ensure_future(self._dock())

    async def _dock(self):
        for _ in range(10):
            await omni.kit.app.get_app().next_update_async()
        for title in ("Console", "Content", "Content Browser", "Property", "Render Settings", "Stage"):
            window = ui.Workspace.get_window(title)
            if window:
                window.visible = False
        viewport = ui.Workspace.get_window("Viewport")
        if viewport and self._window:
            self._window.dock_in(viewport, ui.DockPosition.RIGHT, 0.26)

    def _refresh(self):
        stage = omni.usd.get_context().get_stage()
        if stage != self._stage:
            self._restore_slice_rendering()
            if self._slicer:
                self._slicer.restore()
            if self._visibility:
                self._visibility.restore()
            self._camera.reset_view()
            if self._highlight:
                self._highlight.restore()
            self._highlight = ClusterHighlight(stage) if stage else None
            if self._inspection_session:
                self._inspection_session.restore()
            # Release the old stage only after all session cleanup finishes.
            self._slicer = SectionPlane(stage) if stage else None
            self._inspection_session = (InspectionSession(stage) if stage and not stage.GetRootLayer().anonymous else None)
            self._stage = stage
            self._visibility = SceneVisibility(stage) if stage else None
            self._index = None
            self._site_names.clear()
            self._isolated = False
            self._distance = None
            self._view = "Free"
            self._damage_source_path = None
            self._enabled = {key: True for key in scene_layers(stage)}
        old_path = self._clusters[self._index].path if self._index is not None and self._index < len(self._clusters) else None
        self._clusters = read_clusters(stage)
        self._layers = scene_layers(stage)
        self._scan_sources()
        self._index = next((i for i,c in enumerate(self._clusters) if c.path == old_path), None)
        self._apply_visibility()
        self._rebuild_panels()

    def _rebuild_panels(self):
        self._window.frame.rebuild()
        if self._config_window and self._config_window.visible:
            self._config_window.frame.rebuild()

    def _scan_sources(self):
        from pxr import UsdGeom
        prims = [p for p in self._stage.Traverse() if "__DELTA_" not in str(p.GetPath())] if self._stage else []
        self._all_prim_paths = sorted(str(p.GetPath()) for p in prims)
        self._point_prim_paths = sorted(str(p.GetPath()) for p in prims if p.IsA(UsdGeom.Points))
        if self._damage_source_path not in self._point_prim_paths:
            self._damage_source_path = next((p for token in ("damagedetail", "flagged", "damage", "change")
                                             for p in self._point_prim_paths if token in p.lower()), None)

    def _build_ui(self):
        stage = self._stage
        muted = {"color": 0xFFACA39A, "font_size": 13}
        accent = {"background_color": 0xFFB7A63E, "color": 0xFF211D16,
                  "border_radius": 5, "font_size": 14}
        theme = {
            "Label": {"color": 0xFFECE7E1, "font_size": 14},
            "Button": {"background_color": 0xFF3D342C, "color": 0xFFECE7E1, "border_radius": 5, "font_size": 13},
            "Button:hovered": {"background_color": 0xFF51463B},
            "Button:disabled": {"color": 0xFF81766B, "background_color": 0xFF302921},
            "ComboBox": {"background_color": 0xFF302921, "border_radius": 4},
            "CollapsableFrame": {"background_color": 0xFF29231E, "font_size": 15},
            "CheckBox": {"background_color": 0xFF463C32, "color": 0xFFB7A63E},
        }
        with ui.ScrollingFrame(horizontal_scrollbar_policy=ui.ScrollBarPolicy.SCROLLBAR_ALWAYS_OFF):
            with ui.HStack(spacing=0):
                ui.Spacer(width=16)
                with ui.VStack(height=0, spacing=14, style=theme):
                    ui.Spacer(height=12)
                    ui.Label("DELTA", height=38, style={"font_size": 28, "color": 0xFFB7A63E})
                    name = stage.GetRootLayer().GetDisplayName() if stage else "No inspection loaded"
                    ui.Label(name, tooltip=name, elided_text=True, height=24, style=muted)
                    with ui.HStack(height=36, spacing=8):
                        ui.Button("Open USD", clicked_fn=self._browse)
                        ui.Button("USD Builder", clicked_fn=self._launch_pipeline,
                                  tooltip="Open Callum's unchanged eight-stage processing application.")
                    with ui.HStack(height=32, spacing=8):
                        ui.Label("View", width=45)
                        view = ui.ComboBox(("Free", "Top", "Front", "Right").index(self._view), "Free", "Top", "Front", "Right")
                        view.model.get_item_value_model().add_value_changed_fn(lambda model: self._set_view(("Free", "Top", "Front", "Right")[model.as_int]))
                    self._build_slice_controls(muted)
                    ui.Line(height=1, style={"color": 0xFF51463B})
                    ui.Label(f"{len(self._clusters):,} damage sites", height=30, style={"font_size": 19})
                    names = [self._site_label(c, include_surface=True) for c in self._clusters]
                    combo = ui.ComboBox(self._index + 1 if self._index is not None else 0, *(["Choose a damage site"] + names if names else ["No damage sites found"]), height=34)
                    combo.model.get_item_value_model().add_value_changed_fn(self._on_combo)
                    if self._index is not None:
                        selected = self._clusters[self._index]
                        ui.Label("Temporary name", height=20, style=muted)
                        with ui.HStack(height=30, spacing=6):
                            self._name_model = ui.SimpleStringModel(self._site_names.get(selected.path, ""))
                            ui.StringField(model=self._name_model, tooltip="For example: door. Names last until this scene is closed.")
                            ui.Button("Set", width=52, clicked_fn=self._rename_site)
                            ui.Button("Reset", width=60, clicked_fn=lambda: self._rename_site(reset=True),
                                      enabled=selected.path in self._site_names)
                    with ui.HStack(height=36, spacing=8):
                        ui.Button("Previous", enabled=self._index is not None and self._index > 0,
                                  clicked_fn=lambda: self._step(-1))
                        ui.Label(f"{self._index+1} / {len(self._clusters)}" if self._index is not None else "â€”",
                                 width=68, alignment=ui.Alignment.CENTER, style=muted)
                        ui.Button("Next", enabled=bool(self._clusters) and (self._index is None or self._index < len(self._clusters)-1),
                                  clicked_fn=lambda: self._step(1))
                    ui.Button("Focus on damage", height=42, style=accent, enabled=self._index is not None, clicked_fn=self._focus)
                    with ui.HStack(height=36, spacing=8):
                        ui.Button("Isolate damage", enabled=self._index is not None, clicked_fn=self._isolate)
                        ui.Button("Show all", clicked_fn=self._show_all)
                    ui.Label("Showing selected site" if self._isolated else "Showing full scene", height=20, style=muted)
                    if self._index is not None and not self._clusters[self._index].shape_paths:
                        ui.Label("This file shows damage bounds only.", height=24, style=muted)
                    ui.Spacer(height=8)
                    ui.Button("Configuration", height=34, clicked_fn=self._show_configuration)
                    with ui.CollapsableFrame("Colour key", collapsed=False, height=0):
                        with ui.VStack(height=0, spacing=6):
                            for color, label in (
                                (0xFF808080, "Grey - baseline"),
                                (0xFF0000FF, "Red - positive change"),
                                (0xFFFF0000, "Blue - negative change"),
                                (0xFF0099FF, "Orange - damage-site bounds"),
                                (0xFFD9268C, "Purple - holes"),
                                (0xFFA6B31A, "Teal - unmatched points"),
                            ):
                                with ui.HStack(height=22, spacing=8):
                                    ui.Rectangle(width=18, height=18, style={"background_color": color})
                                    ui.Label(label, style=muted)
                            ui.Label("Change signs follow the export convention.", height=32, word_wrap=True, style=muted)
                    if self._message != "Ready":
                        ui.Label(self._message, word_wrap=True, height=40, style=muted)
                    ui.Spacer(height=8)
                ui.Spacer(width=16)

    def _build_slice_controls(self, muted):
        slicer = self._slicer
        available = bool(slicer and slicer.bounds)
        with ui.CollapsableFrame("Slice / see inside", collapsed=False, height=0):
            with ui.VStack(height=0, spacing=7):
                with ui.HStack(height=24, spacing=6):
                    enabled = ui.CheckBox(width=22, enabled=available)
                    self._slice_enabled_model = enabled.model
                    enabled.model.set_value(bool(slicer and slicer.enabled))
                    enabled.model.add_value_changed_fn(lambda model: self._enable_slice(model.as_bool))
                    ui.Label("Enable slice", tooltip="An invisible plane cuts away one side of the scene. The scan is not edited.")
                if not available:
                    ui.Label("Open a scene to use the slicer.", height=24, style=muted)
                    return
                with ui.HStack(height=30, spacing=6):
                    ui.Label("Direction", width=70)
                    axis = ui.ComboBox(slicer.axis, "X", "Y", "Z")
                    axis.model.get_item_value_model().add_value_changed_fn(self._slice_axis_changed)
                    ui.Button("Flip side", width=90, clicked_fn=self._flip_slice)
                self._slice_model = ui.SimpleFloatModel(slicer.fraction * 100.)
                ui.Label("Cut position (%)", height=20, style=muted)
                ui.FloatSlider(model=self._slice_model, min=0., max=100., step=.1, height=28,
                               tooltip="Move the cutting plane across the scene (0â€“100%). Moving this enables the slice.")
                self._slice_model.add_value_changed_fn(self._slice_position_changed)
                self._slice_position_label = ui.Label(self._slice_description(), height=24, style=muted)
                with ui.HStack(height=30, spacing=6):
                    ui.Button("Through selected site", enabled=self._index is not None, clicked_fn=self._slice_through_site)
                    ui.Button("Reset slice", width=95, clicked_fn=self._reset_slice)

    def _slice_description(self):
        slicer = self._slicer
        unit = self._stage.GetMetadata("metersPerUnit") if self._stage.HasAuthoredMetadata("metersPerUnit") else None
        position = slicer.position * unit if unit is not None else slicer.position
        relation = ">=" if slicer.flipped else "<="
        return f"Keep {'XYZ'[slicer.axis]} {relation} {position:.3f} {'m' if unit is not None else 'scene units'}"

    def _apply_slice(self):
        self._slice_pending = False
        if not self._slicer:
            return
        if self._slicer.enabled:
            if self._slice_render_settings is None:
                self._slice_render_settings = {key: self._settings.get(key) for key in (
                    "/rtx/rendermode", "/rtx/sectionPlane/enabled",
                    "/rtx/sectionPlane/plane", "/rtx/sectionPlane/sectionLight")}
            # The lightweight renderer does not implement mesh section planes.
            self._settings.set("/rtx/rendermode", "RaytracedLighting")
            self._settings.set_float_array("/rtx/sectionPlane/plane", self._slicer.equation)
            self._settings.set_bool("/rtx/sectionPlane/sectionLight", False)
            self._settings.set_bool("/rtx/sectionPlane/enabled", True)
            from omni.kit.viewport.utility import get_active_viewport
            viewport = get_active_viewport()
            self._slicer.apply(viewport.render_product_path if viewport else None)
            self._box_callout.set_clip_plane(self._slicer.equation)
        else:
            self._slicer.restore()
            self._restore_slice_rendering()
            self._box_callout.set_clip_plane(None)
        if self._slice_position_label:
            self._slice_position_label.text = self._slice_description()

    def _restore_slice_rendering(self):
        self._box_callout.set_clip_plane(None)
        if self._slice_render_settings is not None:
            for key, value in self._slice_render_settings.items():
                if value is None:
                    self._settings.destroy_item(key)
                else:
                    self._settings.set(key, value)
            self._slice_render_settings = None

    def _update_slice_viewport(self, _event):
        from omni.kit.viewport.utility import get_active_viewport
        viewport = get_active_viewport()
        if self._inspection_session and not self._inspection_session.locked and self._stage:
            if viewport and viewport.render_product_path and self._stage.GetPrimAtPath(viewport.render_product_path):
                self._inspection_session.lock_when_ready()
        if not self._slicer or not self._slicer.enabled:
            return
        if self._slice_pending:
            self._apply_slice()
            return
        if viewport and str(viewport.render_product_path) != self._slicer._bound_product:
            # A replacement viewport must not leave the old render product clipped.
            self._slicer.restore()
            self._slicer.enabled = True
            self._apply_slice()

    def _enable_slice(self, enabled):
        if self._slice_checkbox_sync:
            return
        if self._slicer:
            self._slicer.enabled = bool(enabled and self._slicer.bounds)
            self._apply_slice()
            model = getattr(self, "_slice_enabled_model", None)
            if model is not None and model.as_bool != self._slicer.enabled:
                self._slice_checkbox_sync = True
                try:
                    model.set_value(self._slicer.enabled)
                finally:
                    self._slice_checkbox_sync = False

    def _slice_axis_changed(self, model):
        self._slicer.axis = model.as_int
        self._apply_slice()
        self._window.frame.rebuild()

    def _slice_position_changed(self, model):
        self._slicer.fraction = max(0., min(1., model.as_float / 100.))
        was_enabled = self._slicer.enabled
        self._slicer.enabled = True
        self._slice_pending = True
        # Keep the slider alive while dragging; update just the checkbox model.
        if not was_enabled:
            self._slice_enabled_model.set_value(True)

    def _flip_slice(self):
        self._slicer.flipped = not self._slicer.flipped
        self._apply_slice()
        self._window.frame.rebuild()

    def _slice_through_site(self):
        if self._index is not None and self._slicer:
            self._slicer.through(self._clusters[self._index].centroid)
            self._enable_slice(True)
            self._window.frame.rebuild()

    def _reset_slice(self):
        if self._slicer:
            self._slicer.fraction = .5
            self._slicer.flipped = False
            self._enable_slice(False)
            self._window.frame.rebuild()

    def _show_configuration(self):
        if self._config_window is None:
            self._config_window = ui.Window("DELTA Configuration", width=540, height=760)
            self._config_window.frame.set_build_fn(self._build_configuration)
        self._config_window.visible = True
        self._config_window.frame.rebuild()

    def _build_configuration(self):
        with ui.ScrollingFrame(horizontal_scrollbar_policy=ui.ScrollBarPolicy.SCROLLBAR_ALWAYS_OFF):
            with ui.VStack(height=0, spacing=12):
                with ui.CollapsableFrame("Prim Navigator", collapsed=False, height=0):
                    with ui.VStack(height=0, spacing=6):
                        self._prim_combo = ui.ComboBox(0, *(self._all_prim_paths or ["No prims available"]), height=30)
                        with ui.HStack(height=34, spacing=6):
                            ui.Button("Select prim", clicked_fn=self._select_prim, enabled=bool(self._all_prim_paths))
                            ui.Button("Clear selection", clicked_fn=self._clear_selection)
                with ui.CollapsableFrame("Automatic Damage Detection", collapsed=False, height=0):
                    with ui.VStack(height=0, spacing=6):
                        ui.Label(f"{len(self._clusters):,} exported damage sites detected", height=24)
                        ui.Label("Navigation follows the damage sites exported by the pipeline. Choose a Points source below to inspect its point count.", height=48, word_wrap=True)
                        index = self._point_prim_paths.index(self._damage_source_path) + 1 if self._damage_source_path in self._point_prim_paths else 0
                        source = ui.ComboBox(index, "No Points source selected", *self._point_prim_paths, height=30)
                        source.model.get_item_value_model().add_value_changed_fn(self._on_source_changed)
                        self._source_info = ui.Label(self._source_description(), height=58, word_wrap=True)
                with ui.CollapsableFrame("Scene layers", collapsed=False, height=0):
                    with ui.VStack(height=0, spacing=6):
                        for key, paths in self._layers.items():
                            if not paths:
                                continue
                            with ui.HStack(height=24, spacing=6):
                                box = ui.CheckBox(width=22)
                                box.model.set_value(self._enabled.get(key, True))
                                box.model.add_value_changed_fn(lambda model, key=key: self._toggle_layer(key, model.as_bool))
                                ui.Label("Damage boxes" if key == "Bounds" else key)
                if self._index is not None:
                    c = self._clusters[self._index]
                    with ui.CollapsableFrame("Selected damage site", collapsed=False, height=0):
                        with ui.VStack(height=0, spacing=6):
                            ui.Label(f"{self._site_label(c)}: {c.point_count:,} member points", height=24)
                            summary = site_summary(self._stage, c.path)
                            for title, key in (("Severity", "severity"), ("Baseline change", "baseline"), ("Peak displacement", "maximum"), ("Damage type", "kind"), ("Surface", "surface")):
                                ui.Label(f"{title}: {summary[key]}", height=28, word_wrap=True)
                            ui.Label("Baseline values describe exported displacement, not change in area. Severity is shown only when supplied by the export.", height=48, word_wrap=True)
                            ui.Label(f"Dimensions: {c.extent[0]:.3f} Ã— {c.extent[1]:.3f} Ã— {c.extent[2]:.3f}", height=24)
                            ui.Label("Dimensions use scene units" if not self._stage.HasAuthoredMetadata("metersPerUnit")
                                     else f"1 scene unit = {self._stage.GetMetadata('metersPerUnit'):g} m", height=24)
                ui.Label(pipeline_status(), word_wrap=True, height=48)
                ui.Label("Viewer frame cap: 30 FPS", height=24)
                ui.Button("Refresh scene", height=32, clicked_fn=self._refresh)

    def _source_description(self):
        prim = self._stage.GetPrimAtPath(self._damage_source_path) if self._stage and self._damage_source_path else None
        points = prim.GetAttribute("points").Get() if prim else None
        return f"Points: {len(points) if points is not None else 0:,}\nSource: {self._damage_source_path or 'None'}"

    def _on_source_changed(self, model):
        index = model.as_int - 1
        self._damage_source_path = self._point_prim_paths[index] if 0 <= index < len(self._point_prim_paths) else None
        self._source_info.text = self._source_description()

    def _select_prim(self):
        index = self._prim_combo.model.get_item_value_model().as_int
        if 0 <= index < len(self._all_prim_paths):
            omni.usd.get_context().get_selection().set_selected_prim_paths([self._all_prim_paths[index]], True)

    def _clear_selection(self):
        omni.usd.get_context().get_selection().set_selected_prim_paths([], True)

    def _site_label(self, cluster, include_surface=False):
        identity = f"Site {cluster.cluster_id:02d}"
        name = self._site_names.get(cluster.path)
        label = f"{name} ({identity})" if name else identity
        return label + (f" / {cluster.surface}" if include_surface and cluster.surface else "")

    def _rename_site(self, reset=False):
        if self._index is None:
            return
        cluster = self._clusters[self._index]
        name = "" if reset else " ".join(self._name_model.as_string.split())
        if name:
            self._site_names[cluster.path] = name
        else:
            self._site_names.pop(cluster.path, None)
        self._box_callout.set_cluster(self._stage, cluster.path, self._site_label(cluster), self._enabled.get("Bounds", True))
        self._rebuild_panels()

    def _on_combo(self, model):
        if model.as_int == 0:
            self._show_all()
        else:
            self._select(model.as_int - 1)

    def _keep_select(self, *_args):
        if self._settings.get("/app/transform/operation") != "select":
            self._settings.set("/app/transform/operation", "select")

    def _step(self, offset):
        if self._index is None and offset > 0:
            self._select(0)
        elif self._index is not None:
            self._select(self._index + offset)

    def _select(self, index, update_scene=True):
        if not 0 <= index < len(self._clusters):
            return
        self._index = index
        if update_scene:
            self._changing_selection = True
            omni.usd.get_context().get_selection().set_selected_prim_paths([self._clusters[index].path], True)
            self._changing_selection = False
        self._apply_visibility()
        self._focus()
        self._rebuild_panels()

    def _view_direction(self):
        from pxr import UsdGeom
        y_up = self._stage and UsdGeom.GetStageUpAxis(self._stage) == UsdGeom.Tokens.y
        return {"Top": (0,1,0) if y_up else (0,0,1),
                "Front": (0,0,1) if y_up else (0,-1,0),
                "Right": (1,0,0)}.get(self._view)

    def _set_view(self, view):
        self._view = view
        if view == "Free":
            self._camera.reset_view()
        elif self._index is not None:
            self._focus()
        elif self._stage:
            from pxr import Usd, UsdGeom
            root = self._stage.GetDefaultPrim() or self._stage.GetPrimAtPath("/World")
            if root:
                bounds = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_, UsdGeom.Tokens.render]).ComputeWorldBound(root).ComputeAlignedRange()
                if not bounds.IsEmpty():
                    target = bounds.GetMidpoint()
                    distance = max(1.0, bounds.GetSize().GetLength() * 1.2)
                    self._camera.move_to(self._stage, target, distance, duration=0.45, direction=self._view_direction())
        self._window.frame.rebuild()

    def _focus(self):
        if self._index is None:
            return
        c = self._clusters[self._index]
        if self._distance is None:
            self._distance = max(1.0, sum(v*v for v in c.extent)**0.5 * 1.8)
        # Reuse the operator's zoom after the first focus.
        from omni.kit.viewport.utility import get_active_viewport
        viewport = get_active_viewport()
        if viewport and viewport.camera_path == self._camera.CAMERA_PATH:
            from pxr import Gf
            distance = (viewport.transform.ExtractTranslation() - Gf.Vec3d(*getattr(self, '_last_centroid', c.centroid))).GetLength()
            if distance > 0.01:
                self._distance = distance
        self._last_centroid = c.centroid
        self._camera.move_to(self._stage, c.centroid, self._distance, duration=0.45, direction=self._view_direction())

    def _toggle_layer(self, key, visible):
        self._enabled[key] = visible
        self._apply_visibility()

    def _apply_visibility(self):
        if not self._visibility:
            return
        selected = self._clusters[self._index].path if self._index is not None else None
        if self._highlight:
            self._highlight.apply(self._clusters, selected, self._enabled.get("Bounds", True))
        label=self._site_label(self._clusters[self._index]) if self._index is not None else ""
        self._box_callout.set_cluster(self._stage, selected, label, self._enabled.get("Bounds", True))
        shape_paths = {p for c in self._clusters for p in c.shape_paths}
        for key, paths in self._layers.items():
            for path in paths:
                visible = self._enabled.get(key, True)
                if self._isolated:
                    visible = visible and bool(selected and path.startswith(selected + "/"))
                elif key in ("Damage points", "Damage mesh") and path in shape_paths:
                    # The shared damage layer already renders these shapes.
                    # Avoid drawing duplicate geometry until a site is isolated.
                    visible = False
                self._visibility.set(path, visible)
        for c in self._clusters:
            self._visibility.set(c.path, not self._isolated or c.path == selected)

    def _isolate(self):
        self._isolated = True
        self._apply_visibility()
        self._message = "Ready"
        self._rebuild_panels()

    def _show_all(self):
        self._index = None
        self._changing_selection = True
        try:
            omni.usd.get_context().get_selection().set_selected_prim_paths([], True)
        finally:
            self._changing_selection = False
        self._isolated = False
        self._message = "Ready"
        self._enabled = {key: True for key in self._layers}
        self._apply_visibility()
        self._rebuild_panels()

    def _on_stage_event(self, event):
        closing = getattr(omni.usd.StageEventType, "CLOSING", None)
        if (closing is not None and event.type == int(closing)) or event.type == int(omni.usd.StageEventType.CLOSED):
            if self._slicer:
                self._slicer.restore()
            self._restore_slice_rendering()
            self._camera.stop()
            self._box_callout.set_cluster(None,None,"")
        if event.type in (int(omni.usd.StageEventType.OPENED), int(omni.usd.StageEventType.CLOSED)):
            asyncio.ensure_future(self._refresh_next())
        elif event.type == int(omni.usd.StageEventType.SELECTION_CHANGED) and not self._changing_selection:
            paths = omni.usd.get_context().get_selection().get_selected_prim_paths()
            for i,c in enumerate(self._clusters):
                if any(path == c.path or path.startswith(c.path + "/") for path in paths):
                    if i != self._index:
                        self._select(i, update_scene=False)
                    break
            else:
                if self._index is not None:
                    self._index = None
                    self._isolated = False
                    self._apply_visibility()
                    self._rebuild_panels()

    async def _refresh_next(self):
        await omni.kit.app.get_app().next_update_async()
        if self._window:
            self._refresh()

    def _browse(self):
        def apply(filename, dirname):
            self._picker.hide()
            asyncio.ensure_future(self._open(os.path.join(dirname, filename)))
        if self._picker:
            self._picker.destroy()
        self._picker = FilePickerDialog("Open inspection USD", apply_button_label="Open", click_apply_handler=apply,
            click_cancel_handler=lambda *_: self._picker.hide(),
            item_filter_options=["USD scenes"],
            item_filter_fn=lambda item: not item or item.is_folder or Path(item.path).suffix.lower() in (".usd", ".usda", ".usdc", ".usdz"))
        self._picker.show()

    async def _open(self, path):
        ok, error = await omni.usd.get_context().open_stage_async(path)
        self._message = "Scene opened" if ok else f"Could not open scene: {error}"
        self._refresh()

    def _launch_pipeline(self):
        try:
            if self._pipeline_process and self._pipeline_process.poll() is None:
                self._message = "Processing pipeline is already open."
            else:
                self._pipeline_process = launch_pipeline()
                self._message = "Processing pipeline opened in a separate window."
        except Exception as exc:
            self._message = str(exc)
        self._window.frame.rebuild()

    def on_shutdown(self):
        self._workspace_task.cancel()
        self._subscription = None
        self._slice_subscription = None
        if self._slicer:
            self._slicer.restore()
        self._restore_slice_rendering()
        if self._visibility:
            self._visibility.restore()
        self._camera.reset_view()
        self._box_callout.destroy()
        if self._inspection_session:
            self._inspection_session.restore()
        self._settings.unsubscribe_to_change_events(self._operation_sub)
        if self._original_operation is not None:
            self._settings.set("/app/transform/operation", self._original_operation)
        if self._original_tools is not None:
            self._settings.set("/exts/omni.kit.manipulator.prim.core/tools/enabled", self._original_tools)
        if self._highlight:
            self._highlight.restore()
        if self._picker:
            self._picker.destroy()
        if self._config_window:
            self._config_window.destroy()
            self._config_window = None
        self._window.destroy()
        self._window = None
