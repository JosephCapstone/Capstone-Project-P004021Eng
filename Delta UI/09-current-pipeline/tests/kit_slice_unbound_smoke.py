"""Exercise the actual RTX renderer with points and meshes on both sides."""

import asyncio
import gc
import json
import traceback
import warnings
from pathlib import Path
import omni.kit.app
import omni.usd
from pxr import Gf, Sdf, Usd, UsdGeom, UsdShade
from omni.kit.viewport.utility import get_active_viewport, capture_viewport_to_file

ROOT = Path(__file__).resolve().parent


def fixture():
    path = ROOT / "slice_fixture.usda"
    stage = (
        Usd.Stage.CreateNew(str(path))
        if not path.exists()
        else Usd.Stage.Open(str(path))
    )
    stage.GetRootLayer().Clear()
    world = UsdGeom.Xform.Define(stage, "/World")
    stage.SetDefaultPrim(world.GetPrim())
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)
    for side, x in (("Left", -2.0), ("Right", 2.0)):
        for kind, z in (("Mesh", 1.2), ("Points", -1.2)):
            color = {
                ("Left", "Mesh"): (1.0, 0.0, 0.0),
                ("Right", "Mesh"): (0.0, 1.0, 0.0),
                ("Left", "Points"): (0.0, 0.0, 1.0),
                ("Right", "Points"): (1.0, 1.0, 0.0),
            }[(side, kind)]
            path = f"/World/{side}{kind}"
            if kind == "Mesh":
                obj = UsdGeom.Mesh.Define(stage, path)
                obj.CreatePointsAttr([
                    (-.75, -.75, -.75), (.75, -.75, -.75),
                    (.75, .75, -.75), (-.75, .75, -.75),
                    (-.75, -.75, .75), (.75, -.75, .75),
                    (.75, .75, .75), (-.75, .75, .75),
                ])
                obj.CreateFaceVertexCountsAttr([4] * 6)
                obj.CreateFaceVertexIndicesAttr([
                    0, 3, 2, 1, 4, 5, 6, 7, 0, 1, 5, 4,
                    1, 2, 6, 5, 2, 3, 7, 6, 3, 0, 4, 7,
                ])
                obj.CreateSubdivisionSchemeAttr("none")
                obj.AddTranslateOp().Set(Gf.Vec3d(x, 0, z))
            else:
                obj = UsdGeom.Points.Define(stage, path)
                obj.CreatePointsAttr(
                    [
                        (x + dx * 0.3, 0.0, z + dz * 0.3)
                        for dx in (-1, 0, 1)
                        for dz in (-1, 0, 1)
                    ]
                )
                obj.CreateWidthsAttr([0.22] * 9)
            obj.CreateDisplayColorAttr([Gf.Vec3f(*color)])
            material = UsdShade.Material.Define(stage, path + "Material")
            shader = UsdShade.Shader.Define(stage, str(material.GetPath()) + "/Surface")
            shader.CreateIdAttr("UsdPreviewSurface")
            shader.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).Set(
                Gf.Vec3f(*color)
            )
            shader.CreateInput("emissiveColor", Sdf.ValueTypeNames.Color3f).Set(
                Gf.Vec3f(*color)
            )
            material.CreateSurfaceOutput().ConnectToSource(
                shader.ConnectableAPI(), "surface"
            )
            pass # Match exported unbound displayColor mesh
    camera = UsdGeom.Camera.Define(stage, "/World/TestCamera")
    matrix = Gf.Matrix4d(1.0)
    matrix.SetLookAt(Gf.Vec3d(0, -16, 4), Gf.Vec3d(0, 0, 0), Gf.Vec3d(0, 0, 1))
    camera.AddTransformOp().Set(matrix.GetInverse())
    site = UsdGeom.Xform.Define(stage, "/World/DamageSites/Site_00").GetPrim()
    site.CreateAttribute("delta:clusterId", Sdf.ValueTypeNames.Int).Set(0)
    site.CreateAttribute("delta:centroid", Sdf.ValueTypeNames.Double3).Set(
        Gf.Vec3d(1, 0, 0)
    )
    stage.GetRootLayer().Save()
    return stage.GetRootLayer().identifier


async def smoke():
    result = {"ok": False}
    app = omni.kit.app.get_app()
    try:
        for _ in range(30):
            await app.next_update_async()
        context = omni.usd.get_context()
        print("SLICE_TEST: opening fixture", flush=True)
        ok, error = await context.open_stage_async(fixture())
        assert ok, error
        for _ in range(10):
            await app.next_update_async()
        import delta.robot.ui.extension as module
        from delta.robot.ui.section_plane import PREFIX

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", FutureWarning)
            ext = next(
                x
                for x in gc.get_objects()
                if isinstance(x, module.Extension) and getattr(x, "_window", None)
            )
        ext._refresh()
        print("SLICE_TEST: fixture ready", flush=True)
        stage = context.get_stage()
        source_layer = stage.GetRootLayer()
        viewport = get_active_viewport()
        with Usd.EditContext(stage, stage.GetSessionLayer()):
            stage.GetPrimAtPath(viewport.render_product_path).CreateRelationship(
                "camera"
            ).SetTargets([Sdf.Path("/World/TestCamera")])
            viewport.camera_path = "/World/TestCamera"
        slicer = ext._slicer
        assert slicer.bounds and not slicer.enabled
        slicer.axis = 0

        async def capture(name):
            for _ in range(8):
                await app.next_update_async()
            await capture_viewport_to_file(
                viewport, str(ROOT / f"slice_{name}.png")
            ).wait_for_result()

        await capture("off")
        print("SLICE_TEST: enabling", flush=True)
        original = stage.GetRootLayer().ExportToString()
        ext._enable_slice(True)
        import delta.robot.ui.section_plane as sm
        print('DEBUG_SLICE', sm.__file__, 'face clipping', flush=True)
        print('DEBUG_BIND', UsdShade.MaterialBindingAPI(stage.GetPrimAtPath('/World/LeftMesh')).ComputeBoundMaterial(), flush=True)
        await capture("left")
        print("SLICE_TEST: left captured", flush=True)
        product = stage.GetPrimAtPath(viewport.render_product_path)
        assert product.GetAttribute(PREFIX + "enabled").Get()
        assert list(product.GetAttribute(PREFIX + "plane").Get()) == [
            -1.0,
            0.0,
            0.0,
            0.0,
        ]
        ext._flip_slice()
        await capture("right")
        print("SLICE_TEST: right captured", flush=True)
        # Moving the slider enables slicing and moves the actual render plane.
        ext._enable_slice(False)
        ext._slice_model.set_value(25.0)
        await app.next_update_async()
        assert slicer.enabled and abs(slicer.fraction - 0.25) < 1e-6
        assert (
            abs(product.GetAttribute(PREFIX + "plane").Get()[3] + slicer.position)
            < 1e-5
        )
        # Through selected site, unit-aware label, all three axes, and reset.
        ext._index = 0
        ext._slice_through_site()
        assert abs(slicer.position - 1.0) < 1e-5
        assert "m" in ext._slice_description()
        import omni.ui as ui

        for axis in (1, 2, 0):
            ext._slice_axis_changed(ui.SimpleIntModel(axis))
            assert abs(product.GetAttribute(PREFIX + "plane").Get()[axis]) == 1.0
        ext._reset_slice()
        assert not slicer.enabled
        await capture("restored")
        print("SLICE_TEST: restored captured", flush=True)
        if stage.GetRootLayer().ExportToString() != original:
            import difflib

            raise AssertionError(
                "Source changed:\n"
                + "\n".join(
                    difflib.unified_diff(
                        original.splitlines(),
                        stage.GetRootLayer().ExportToString().splitlines(),
                    )
                )
            )
        from omni.kit.renderer import capture as renderer_capture

        renderer = renderer_capture.acquire_renderer_capture_interface()
        renderer.capture_next_frame_swapchain(str(ROOT / "slice_controls_preview.png"))
        await app.next_update_async()
        renderer.wait_async_capture()
        ext._enable_slice(True)
        await context.new_stage_async()
        for _ in range(10):
            await app.next_update_async()
        assert ext._slicer and not ext._slicer.enabled
        assert source_layer.ExportToString() == original
        result = {
            "ok": True,
            "render_captures": ["off", "left", "right", "restored"],
            "slider_enables_slice": True,
            "flip": True,
            "three_axes": True,
            "through_selected_site": True,
            "reset": True,
            "new_scene_resets_slice": True,
            "source_unchanged": True,
        }
    except Exception:
        result["error"] = traceback.format_exc()
    (ROOT / "kit_slice_unbound_smoke_result.json").write_text(json.dumps(result, indent=2))
    print("DELTA_SLICE_RESULT", result, flush=True)
    app.post_quit(0 if result["ok"] else 1)


asyncio.ensure_future(smoke())


