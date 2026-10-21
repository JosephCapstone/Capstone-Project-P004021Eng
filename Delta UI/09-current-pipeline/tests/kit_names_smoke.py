"""Exercise temporary naming with real Kit UI and two small synthetic sites."""

import asyncio
import gc
import json
from pathlib import Path
import traceback
import warnings
import omni.kit.app
import omni.usd
from pxr import Gf, Sdf, UsdGeom


async def smoke():
    app = omni.kit.app.get_app()
    result = {"ok": False}
    try:
        for _ in range(30):
            await app.next_update_async()
        context = omni.usd.get_context()
        await context.new_stage_async()
        for _ in range(10):
            await app.next_update_async()
        import delta.robot.ui.extension as module

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", FutureWarning)
            ext = next(
                x
                for x in gc.get_objects()
                if isinstance(x, module.Extension) and getattr(x, "_window", None)
            )
        stage = context.get_stage()
        for index in range(2):
            path = f"/World/Scan/DamageSites/Site_{index:02d}"
            prim = UsdGeom.Xform.Define(stage, path).GetPrim()
            prim.CreateAttribute("delta:clusterId", Sdf.ValueTypeNames.Int).Set(index)
            prim.CreateAttribute("delta:centroid", Sdf.ValueTypeNames.Double3).Set(
                Gf.Vec3d(index * 3, 0, 0)
            )
            UsdGeom.Cube.Define(stage, path + "/Bounds")
        fixture = Path(__file__).parent / "names_fixture.usda"
        stage.GetRootLayer().Export(str(fixture))
        ok, error = await context.open_stage_async(str(fixture))
        assert ok, error
        for _ in range(10):
            await app.next_update_async()
        stage = context.get_stage()
        ext._refresh()
        for _ in range(3):
            await app.next_update_async()
        original = stage.GetRootLayer().ExportToString()
        ext._select(0)
        for _ in range(3):
            await app.next_update_async()
        print("NAMES_TEST: typing temporary name", flush=True)
        ext._name_model.set_value("  door  ")
        ext._rename_site()
        assert ext._site_label(ext._clusters[0]) == "door (Site 00)"
        assert ext._box_callout._text == "door (Site 00)"
        ext._select(1)
        ext._select(0)
        for _ in range(3):
            await app.next_update_async()
        assert ext._name_model.as_string == "door"
        ext._refresh()
        assert ext._site_names[ext._clusters[0].path] == "door"
        ext._rename_site(reset=True)
        assert ext._site_label(ext._clusters[0]) == "Site 00"
        ext._name_model.set_value("door")
        ext._rename_site()
        for _ in range(5):
            await app.next_update_async()
        current = stage.GetRootLayer().ExportToString()
        if current != original:
            import difflib

            raise AssertionError(
                "Source changed:\n"
                + "\n".join(
                    difflib.unified_diff(original.splitlines(), current.splitlines())
                )
            )
        print("NAMES_TEST: checking scene change", flush=True)
        await context.new_stage_async()
        for _ in range(10):
            await app.next_update_async()
        assert ext._site_names == {}
        result = {
            "ok": True,
            "temporary_site_names": True,
            "navigation_and_refresh": True,
            "reset": True,
            "new_scene_clears_names": True,
            "root_layer_unchanged": True,
        }
    except Exception:
        result["error"] = traceback.format_exc()
    (Path(__file__).parent / "kit_names_smoke_result.json").write_text(
        json.dumps(result, indent=2)
    )
    print("DELTA_NAMES_RESULT", result, flush=True)
    app.post_quit(0 if result["ok"] else 1)


asyncio.ensure_future(smoke())
