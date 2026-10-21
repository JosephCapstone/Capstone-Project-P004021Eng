import sys
import unittest
from pathlib import Path
from pxr import Gf, Sdf, Usd, UsdGeom

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "exts/delta.robot.ui/delta/robot/ui")
)
from section_plane import PREFIX, SectionPlane


class SectionPlaneTests(unittest.TestCase):
    def fixture(self):
        stage = Usd.Stage.CreateInMemory()
        UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
        group = UsdGeom.Xform.Define(stage, "/World/Scan")
        group.AddTranslateOp().Set(Gf.Vec3d(10, 20, 30))
        UsdGeom.Cube.Define(stage, "/World/Scan/Mesh")
        product = stage.DefinePrim("/Render/Product", "RenderProduct")
        return stage, product

    def test_plane_uses_world_bounds_and_flip_keeps_same_position(self):
        stage, _ = self.fixture()
        slicer = SectionPlane(stage)
        self.assertEqual(slicer.bounds, ((9.0, 19.0, 29.0), (11.0, 21.0, 31.0)))
        self.assertEqual(slicer.equation, [0.0, 0.0, -1.0, 30.0])
        slicer.flipped = True
        self.assertEqual(slicer.equation, [0.0, 0.0, 1.0, -30.0])
        slicer.axis = 0
        slicer.fraction = 0.25
        self.assertEqual(slicer.equation, [1.0, 0.0, 0.0, -9.5])

    def test_source_and_geometry_unchanged_and_session_opinions_restored(self):
        stage, product = self.fixture()
        name = PREFIX + "plane"
        product.CreateAttribute(name, Sdf.ValueTypeNames.FloatArray).Set(
            [1.0, 0.0, 0.0, 4.0]
        )
        with Usd.EditContext(stage, stage.GetSessionLayer()):
            product.GetAttribute(name).Set([0.0, 1.0, 0.0, 5.0])
            product.GetAttribute(name).SetCustomDataByKey("test", "preserve")
        source = stage.GetRootLayer().ExportToString()
        before = (
            stage.GetSessionLayer()
            .GetAttributeAtPath(product.GetPath().AppendProperty(name))
            .GetInfo("customData")
        )
        slicer = SectionPlane(stage)
        slicer.enabled = True
        self.assertTrue(slicer.apply(product.GetPath()))
        self.assertTrue(product.GetAttribute(PREFIX + "enabled").Get())
        self.assertTrue(stage.GetPrimAtPath("/__DELTA_SliceFill"))
        slicer.restore()
        self.assertFalse(stage.GetPrimAtPath("/__DELTA_SliceFill"))
        self.assertEqual(list(product.GetAttribute(name).Get()), [0.0, 1.0, 0.0, 5.0])
        self.assertEqual(product.GetAttribute(name).GetCustomData(), before)
        self.assertIsNone(
            stage.GetSessionLayer().GetPropertyAtPath(
                product.GetPath().AppendProperty(PREFIX + "enabled")
            )
        )
        self.assertEqual(stage.GetRootLayer().ExportToString(), source)
        slicer.restore()

    def test_through_site_and_degenerate_axis(self):
        stage, _ = self.fixture()
        slicer = SectionPlane(stage)
        slicer.through((10, 20, 30.5))
        self.assertAlmostEqual(slicer.position, 30.5)
        slicer.through((10, 20, 100))
        self.assertEqual(slicer.fraction, 1.0)
        slicer.bounds = ((0, 0, 0), (1, 1, 0))
        slicer.through((0, 0, 0))
        self.assertEqual(slicer.fraction, 0.5)

    def test_viewport_render_mode_overrides_global_minimal_and_restores(self):
        stage, product = self.fixture()
        name = "omni:rtx:rendermode"
        product.CreateAttribute(name, Sdf.ValueTypeNames.Token).Set("Minimal")
        with Usd.EditContext(stage, stage.GetSessionLayer()):
            product.GetAttribute(name).Set("PathTracing")
        slicer = SectionPlane(stage)
        slicer.enabled = True
        slicer.apply(product.GetPath())
        self.assertEqual(product.GetAttribute(name).Get(), "RaytracedLighting")
        slicer.restore()
        self.assertEqual(product.GetAttribute(name).Get(), "PathTracing")

    def test_empty_scene_does_not_enable_and_missing_viewport_is_safe(self):
        stage = Usd.Stage.CreateInMemory()
        product = stage.DefinePrim("/Render/Product", "RenderProduct")
        slicer = SectionPlane(stage)
        self.assertIsNone(slicer.bounds)
        self.assertFalse(slicer.apply(None))
        slicer.enabled = True
        slicer.apply(product.GetPath())
        self.assertFalse(product.GetAttribute(PREFIX + "enabled").Get())

    def test_point_arrays_stay_aligned_with_transforms_and_indexed_colours(self):
        stage, product = self.fixture()
        points = UsdGeom.Points.Define(stage, "/World/Scan/Points")
        points.CreatePointsAttr([(-1, 0, 0), (1, 0, 0)])
        points.CreateWidthsAttr([0.1, 0.2])
        points.CreateIdsAttr([100, 200])
        color = UsdGeom.PrimvarsAPI(points).CreatePrimvar(
            "displayColor", Sdf.ValueTypeNames.Color3fArray, UsdGeom.Tokens.vertex
        )
        color.Set([(1, 0, 0), (0, 1, 0)])
        color.SetIndices([1, 0])
        weight = UsdGeom.PrimvarsAPI(points).CreatePrimvar(
            "weight", Sdf.ValueTypeNames.FloatArray, UsdGeom.Tokens.vertex
        )
        weight.Set([0.3, 0.7])
        source = stage.GetRootLayer().ExportToString()
        slicer = SectionPlane(stage)
        slicer.axis = 0
        slicer.enabled = True
        slicer.apply(product.GetPath())
        self.assertEqual(list(points.GetPointsAttr().Get()), [Gf.Vec3f(-1, 0, 0)])
        self.assertEqual(list(points.GetIdsAttr().Get()), [100])
        self.assertEqual(list(color.GetIndices()), [1])
        self.assertEqual(len(color.Get()), 2)
        self.assertAlmostEqual(weight.Get()[0], 0.3)
        slicer.flipped = True
        slicer.apply(product.GetPath())
        self.assertEqual(list(points.GetIdsAttr().Get()), [200])
        self.assertEqual(list(color.GetIndices()), [0])
        slicer.restore()
        self.assertEqual(list(points.GetIdsAttr().Get()), [100, 200])
        self.assertEqual(list(color.GetIndices()), [1, 0])
        self.assertEqual(stage.GetRootLayer().ExportToString(), source)

    def test_point_session_overrides_are_restored_exactly(self):
        stage, product = self.fixture()
        points = UsdGeom.Points.Define(stage, "/World/Points")
        points.CreatePointsAttr([(-1, 0, 0), (1, 0, 0)])
        with Usd.EditContext(stage, stage.GetSessionLayer()):
            points.GetPointsAttr().Set([(-2, 0, 0), (2, 0, 0)])
        original = list(points.GetPointsAttr().Get())
        slicer = SectionPlane(stage)
        slicer.axis = 0
        slicer.enabled = True
        slicer.apply(product.GetPath())
        slicer.restore()
        self.assertEqual(list(points.GetPointsAttr().Get()), original)


if __name__ == "__main__":
    unittest.main()
