"""Render the actual exported mesh alone to expose unclipped surface geometry."""
import asyncio
import gc
import json
import traceback
from pathlib import Path
import omni.kit.app
import omni.usd
from pxr import Gf, Sdf, Usd, UsdGeom, UsdShade
from omni.kit.viewport.utility import get_active_viewport, capture_viewport_to_file

ROOT = Path(__file__).resolve().parent

async def run():
    app = omni.kit.app.get_app()
    result = {"ok": False}
    try:
        for _ in range(30):
            await app.next_update_async()
        context = omni.usd.get_context()
        ok, error = await context.open_stage_async('C:/Users/dylan/Downloads/Lproj_export_001.usd')
        assert ok, error
        for _ in range(20):
            await app.next_update_async()
        import delta.robot.ui.extension as module
        ext = next(x for x in gc.get_objects() if isinstance(x, module.Extension) and getattr(x, '_window', None))
        ext._refresh()
        stage = context.get_stage()
        viewport = get_active_viewport()
        with Usd.EditContext(stage, stage.GetSessionLayer()):
            for prim in stage.Traverse():
                if prim.IsA(UsdGeom.Points):
                    UsdGeom.Imageable(prim).MakeInvisible()
            low, high = ext._slicer.bounds
            target = Gf.Vec3d(*[(a+b)/2 for a,b in zip(low,high)])
            distance = (Gf.Vec3d(*high)-Gf.Vec3d(*low)).GetLength()*1.5
            camera = UsdGeom.Camera.Define(stage, '/__DELTA_TestCamera')
            matrix = Gf.Matrix4d(1)
            matrix.SetLookAt(target+Gf.Vec3d(0,-distance,distance*.25),target,Gf.Vec3d(0,0,1))
            camera.AddTransformOp().Set(matrix.GetInverse())
            viewport.camera_path = str(camera.GetPath())
            stage.GetPrimAtPath(viewport.render_product_path).CreateRelationship('camera').SetTargets([camera.GetPath()])
        source_path = Path(stage.GetRootLayer().realPath)
        original = source_path.stat().st_mtime_ns
        async def capture(name):
            for _ in range(35):
                await app.next_update_async()
            await capture_viewport_to_file(viewport,str(ROOT / ('real_mesh_'+name+'.png'))).wait_for_result()
        await capture('off')
        ext._slicer.axis=2
        ext._slicer.fraction=.482
        ext._enable_slice(True)
        mesh = stage.GetPrimAtPath('/World/Compartment/ChangeHighlight')
        print('REAL_SLICE_DEBUG', ext._slicer.equation, mesh.GetAttribute("holeIndices").GetNumTimeSamples(),
              UsdShade.MaterialBindingAPI(mesh).ComputeBoundMaterial(),
              mesh.GetAttribute('subdivisionScheme').Get(),
              viewport.render_product_path, flush=True)
        await capture('cut')
        ext._flip_slice()
        await capture('flipped')
        ext._reset_slice()
        await capture('restored')
        assert source_path.stat().st_mtime_ns==original
        result={'ok':True,'source_unchanged':True}
    except Exception:
        result['error']=traceback.format_exc()
    (ROOT/'kit_real_mesh_slice_result.json').write_text(json.dumps(result,indent=2))
    app.post_quit(0 if result['ok'] else 1)

asyncio.ensure_future(run())

