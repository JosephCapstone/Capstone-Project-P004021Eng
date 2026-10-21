import importlib.util
from pathlib import Path
import unittest
from pxr import Usd, UsdGeom, Sdf, Gf

p = (
    Path(__file__).resolve().parents[1]
    / "exts/delta.robot.ui/delta/robot/ui/damage_summary.py"
)
spec = importlib.util.spec_from_file_location("summary", p)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class SummaryTests(unittest.TestCase):
    def test_missing_and_signed_values(self):
        stage = Usd.Stage.CreateInMemory()
        prim = UsdGeom.Xform.Define(stage, "/Site").GetPrim()
        original = stage.GetRootLayer().ExportToString()
        self.assertEqual(m.site_summary(stage, "/Site")["severity"], "Not assessed")
        self.assertEqual(stage.GetRootLayer().ExportToString(), original)
        prim.CreateAttribute("delta:meanChangeInward", Sdf.ValueTypeNames.Float).Set(
            -0.012
        )
        prim.CreateAttribute("delta:extent", Sdf.ValueTypeNames.Float3).Set(
            Gf.Vec3f(1, 2, 0.1)
        )
        result = m.site_summary(stage, "/Site")
        self.assertIn("-12.0 mm", result["baseline"])
        self.assertIn("removed", result["baseline"])
        self.assertEqual(result["dimensions"], "1.000 × 2.000 × 0.100 m (XYZ)")

    def test_card_stays_inside_and_flips(self):
        x, y = m.card_position((0.8, -0.9, 0.95, 0.9), (1000, 600))
        self.assertLess(x, 900 - 280)
        self.assertGreaterEqual(y, 12)
        for bounds in ((-1, -1, -0.8, -0.8), (0.9, 0.9, 1, 1)):
            x, y = m.card_position(bounds, (1000, 600))
            self.assertTrue(12 <= x <= 708)
            self.assertTrue(12 <= y <= 404)
