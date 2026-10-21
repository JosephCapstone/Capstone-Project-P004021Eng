"""Check the shared readers preserve measurement-field selection and PLY loading."""

import sys
import tempfile
import unittest
from pathlib import Path
import numpy as np
from plyfile import PlyData, PlyElement

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline/Scripts"))
from cloud_io import find_distance_field, find_change_field, load_cloud


class CloudIOTests(unittest.TestCase):
    def test_distance_does_not_select_uncertainty(self):
        vertex = np.zeros(
            1, dtype=[("distance_uncertainty", "f4"), ("M3C2_distance", "f4")]
        )
        self.assertEqual(
            find_distance_field(PlyElement.describe(vertex, "vertex")), "M3C2_distance"
        )

    def test_change_field_keeps_direction_when_present(self):
        vertex = np.zeros(1, dtype=[("M3C2_distance", "f4"), ("change_inward", "f4")])
        element = PlyElement.describe(vertex, "vertex")
        self.assertEqual(find_change_field(element), "change_inward")
        self.assertEqual(find_distance_field(element), "M3C2_distance")

    def test_real_ply_load_and_empty_rejection(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "cloud.ply"
            vertex = np.zeros(3, dtype=[("x", "f4"), ("y", "f4"), ("z", "f4")])
            vertex["x"] = [0, 1, 2]
            PlyData([PlyElement.describe(vertex, "vertex")], text=False).write(
                str(path)
            )
            cloud = load_cloud(path)
            np.testing.assert_allclose(np.asarray(cloud.points)[:, 0], [0, 1, 2])
            PlyData([PlyElement.describe(vertex[:0], "vertex")], text=False).write(
                str(path)
            )
            with self.assertRaises(ValueError):
                load_cloud(path)
