import asyncio
import json
from pathlib import Path
import traceback
import omni.kit.app
import omni.usd


def assert_source_unchanged(stage, original):
    current = stage.GetRootLayer().ExportToString()
    if current != original:
        import difflib
        from itertools import islice

        difference = "\n".join(
            line[:500]
            for line in islice(
                difflib.unified_diff(original.splitlines(), current.splitlines()), 60
            )
        )
        raise AssertionError("Source layer changed:\n" + difference)


async def smoke():
    result = {"ok": False}
    app = omni.kit.app.get_app()
    try:
        for _ in range(30):
            await app.next_update_async()
        context = omni.usd.get_context()
        ok, error = await context.open_stage_async(
            str(Path.home() / "Downloads/qtv2test_export_002.usd")
        )
        assert ok, error
        for _ in range(10):
            await app.next_update_async()
        import delta.robot.ui.extension as module
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", FutureWarning)
            instances = [
                x
                for x in __import__("gc").get_objects()
                if isinstance(x, module.Extension)
                and getattr(x, "_window", None) is not None
            ]
        ext = instances[0]
        assert len(ext._clusters) == 290
        assert (
            context.get_stage().GetEditTarget().GetLayer()
            == context.get_stage().GetSessionLayer()
        )
        import carb.settings

        settings = carb.settings.get_settings()
        for loop in ("main", "present", "rendering_0"):
            assert settings.get(f"/app/runLoops/{loop}/rateLimitEnabled") is True
            assert settings.get(f"/app/runLoops/{loop}/rateLimitFrequency") == 30
        assert settings.get("/app/runLoops/main/rateLimitUseBusyLoop") is False
        assert abs(settings.get("/rtx/sceneDb/ambientLightIntensity") - 0.08) < 1e-6
        assert settings.get("/rtx/rendermode") == "MinimalRendering"
        assert (
            settings.get("/persistent/exts/omni.kit.viewport.menubar.camera/visible")
            is False
        )
        settings.set("/app/transform/operation", "translate")
        await app.next_update_async()
        assert settings.get("/app/transform/operation") == "select"
        original = context.get_stage().GetRootLayer().ExportToString()
        assert ext._index is None
        ext._step(1)
        assert ext._index == 0
        for view in ("Top", "Front", "Right", "Free"):
            ext._set_view(view)
            assert ext._view == view
            if view != "Free":
                assert ext._camera._subscription is not None
        ext._select(1)
        context.get_selection().set_selected_prim_paths(
            [ext._clusters[5].path + "/Bounds"], True
        )
        for _ in range(10):
            await app.next_update_async()
            if ext._index == 5:
                break
        assert (
            ext._index == 5
        ), f"Viewport/Stage selection must follow the damage site: index={ext._index}, selection={context.get_selection().get_selected_prim_paths()}"
        ext._clear_selection()
        for _ in range(10):
            await app.next_update_async()
            if ext._index is None:
                break
        assert ext._index is None, "Clearing selection must restore normal box opacity"
        ext._select(1)
        ext._step(1)
        assert ext._index == 2
        ext._isolate()
        ext._show_all()
        assert ext._index is None
        ext._select(2)
        assert_source_unchanged(context.get_stage(), original)
        for _ in range(180):
            await app.next_update_async()
            if ext._camera._subscription is None:
                break
        for _ in range(5):
            await app.next_update_async()
        assert any(
            line.visible for line in ext._box_callout._lines
        ), "Selected cluster outline must be visible after focus"
        assert ext._box_callout._card.visible

        site = ext._clusters[ext._index]
        ext._name_model.set_value("  door  ")
        ext._rename_site()
        assert ext._site_names[site.path] == "door"
        assert ext._site_label(site) == f"door (Site {site.cluster_id:02d})"
        assert ext._box_callout._text == ext._site_label(site)
        ext._refresh()
        assert (
            ext._site_names[site.path] == "door"
        ), "Refresh must retain temporary names"
        ext._rename_site(reset=True)
        assert site.path not in ext._site_names
        import omni.ui as ui

        ext._name_model = ui.SimpleStringModel("door")
        ext._rename_site()
        assert_source_unchanged(context.get_stage(), original)

        ext._show_configuration()
        for _ in range(5):
            await app.next_update_async()
        assert ext._config_window.visible and ext._prim_combo is not None
        assert (
            ext._source_info is not None
            and ext._damage_source_path in ext._point_prim_paths
        )
        ext._toggle_layer("Bounds", False)
        assert not ext._box_callout._card.visible
        from pxr import UsdGeom

        assert all(
            UsdGeom.Imageable(
                context.get_stage().GetPrimAtPath(c.path + "/Bounds")
            ).ComputeVisibility()
            == "invisible"
            for c in ext._clusters
        )
        ext._toggle_layer("Bounds", True)
        assert_source_unchanged(context.get_stage(), original)

        from omni.kit.renderer import capture

        renderer = capture.acquire_renderer_capture_interface()
        # Exercise slicing on the real multi-million-point, 290-site export too.
        import time

        slicer = ext._slicer
        slicer.axis = 2
        slicer.fraction = 0.5
        start = time.perf_counter()
        ext._enable_slice(True)
        slice_update_seconds = time.perf_counter() - start
        for _ in range(15):
            await app.next_update_async()
        original_points = sum(len(xyz) for _, xyz, _, _, _ in slicer._point_clouds)
        sliced_points = sum(
            len(prim.GetAttribute("points").Get())
            for prim, _, _, _, _ in slicer._point_clouds
        )
        assert 0 < sliced_points < original_points
        assert ext._box_callout._clip_plane == slicer.equation
        assert_source_unchanged(context.get_stage(), original)
        from pxr import Gf

        target = tuple((a + b) * 0.5 for a, b in zip(*slicer.bounds))
        span = max(b - a for a, b in zip(*slicer.bounds))
        ext._camera.move_to(
            context.get_stage(),
            target,
            max(1.0, span * 1.3),
            duration=0.2,
            direction=(1, -1, 0.7),
        )
        for _ in range(15):
            await app.next_update_async()
        ext._config_window.visible = False
        renderer.capture_next_frame_swapchain(
            str(Path(__file__).resolve().parent / "slice_sample_preview.png")
        )
        await app.next_update_async()
        renderer.wait_async_capture()
        ext._enable_slice(False)
        for _ in range(8):
            await app.next_update_async()
        assert (
            sum(
                len(prim.GetAttribute("points").Get())
                for prim, _, _, _, _ in slicer._point_clouds
            )
            == original_points
        )
        assert settings.get("/rtx/rendermode") == "MinimalRendering"
        assert_source_unchanged(context.get_stage(), original)
        ext._config_window.visible = True
        renderer.capture_next_frame_swapchain(
            str(Path(__file__).resolve().parent / "configuration_preview.png")
        )
        await app.next_update_async()
        renderer.wait_async_capture()
        ext._config_window.visible = False
        for _ in range(3):
            await app.next_update_async()
        renderer.capture_next_frame_swapchain(
            str(Path(__file__).resolve().parent / "ui_preview.png")
        )
        await app.next_update_async()
        renderer.wait_async_capture()
        sample_count = len(ext._clusters)
        await context.new_stage_async()
        for _ in range(5):
            await app.next_update_async()
        assert ext._site_names == {}, "Temporary names must not leak into another scene"
        from pxr import UsdGeom, Sdf, Gf

        stage = context.get_stage()
        site = UsdGeom.Xform.Define(stage, "/World/DamageSites/Site_00")
        prim = site.GetPrim()
        prim.CreateAttribute("delta:clusterId", Sdf.ValueTypeNames.Int).Set(0)
        prim.CreateAttribute("delta:centroid", Sdf.ValueTypeNames.Double3).Set(
            Gf.Vec3d(0, 0, 0)
        )
        shape = UsdGeom.Points.Define(stage, str(prim.GetPath()) + "/ShapePoints")
        shape.CreatePointsAttr([(0, 0, 0), (1, 0, 0)])
        shape.CreateVisibilityAttr("invisible")
        ext._refresh()
        original = stage.GetRootLayer().ExportToString()
        ext._select(0)
        ext._isolate()
        assert shape.ComputeVisibility() == "inherited"
        ext._show_all()
        assert shape.ComputeVisibility() == "invisible"
        assert stage.GetRootLayer().ExportToString() == original
        result = {
            "ok": True,
            "clusters": sample_count,
            "root_layer_unchanged": True,
            "shape_isolation": True,
            "inspection_locked": True,
            "selected_outline_visible": True,
            "quick_info_callout": True,
            "configuration_panel": True,
            "native_site_selection": True,
            "box_visibility": True,
            "frame_cap": 30,
            "ambient_fill": 0.08,
            "renderer": "MinimalRendering",
            "temporary_site_names": True,
            "sample_slicing": {
                "original_points": original_points,
                "visible_points": sliced_points,
                "first_update_seconds": slice_update_seconds,
                "restored": True,
            },
        }
    except Exception:
        result["error"] = traceback.format_exc()
    (Path(__file__).resolve().parent / "kit_smoke_result.json").write_text(
        json.dumps(result, indent=2)
    )
    print("DELTA_SMOKE_RESULT", result)
    app.post_quit(0 if result["ok"] else 1)


asyncio.ensure_future(smoke())
