import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import numpy as np
from plyfile import PlyData, PlyElement
from pxr import Usd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline/Gui"))
sys.path.insert(0, str(ROOT / "exts/delta.robot.ui/delta/robot/ui"))


class PipelineIntegrationTests(unittest.TestCase):
    def test_all_eight_stage_panels_load(self):
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        from PySide6.QtWidgets import QApplication
        from main import PipelineAppletWindow, PANEL_IMPORT_ERRORS, STAGE_PANEL_CLASSES

        app = QApplication.instance() or QApplication([])
        self.assertEqual(PANEL_IMPORT_ERRORS, {})
        window = PipelineAppletWindow()
        for name in STAGE_PANEL_CLASSES:
            window._show_stage(name)
        app.processEvents()
        self.assertEqual(len(window._stage_pages), 8)
        surface = window._stage_pages["Stage 7: Surface"]
        from stage7_surface_dialog_qt import DEFAULT_BLEND_FILE, DEFAULT_BLENDER_SCRIPT

        self.assertTrue(DEFAULT_BLEND_FILE.is_file())
        self.assertTrue(DEFAULT_BLENDER_SCRIPT.is_file())
        window.close()

    def test_blender_command_carries_cluster_membership(self):
        from pipeline_core import build_blender_surface_command

        cmd = build_blender_surface_command(
            "blender.exe",
            "recipe.blend",
            "script.py",
            "input.ply",
            "output.ply",
            carry_field=["scalar_M3C2_distance", "cluster_id"],
            overrides={"Mesh/Voxel Size": 0.025},
        )
        self.assertLess(cmd.index("--python-exit-code"), cmd.index("--python"))
        self.assertIn("cluster_id", cmd)
        self.assertIn("scalar_M3C2_distance", cmd)
        self.assertIn("Mesh/Voxel Size=0.025", cmd)

    def test_original_export_retains_centroids_and_global_mesh(self):
        from cluster_model import read_clusters

        with tempfile.TemporaryDirectory() as temp:
            d = Path(temp)
            vertex = np.zeros(
                6,
                dtype=[
                    ("x", "f4"),
                    ("y", "f4"),
                    ("z", "f4"),
                    ("scalar_M3C2_distance", "f4"),
                    ("cluster_id", "i4"),
                ],
            )
            vertex["x"] = [0, 1, 0, 10, 11, 10]
            vertex["y"] = [0, 0, 1, 0, 0, 1]
            vertex["scalar_M3C2_distance"] = [-0.1] * 3 + [0.2] * 3
            vertex["cluster_id"] = [0] * 3 + [1] * 3
            PlyData([PlyElement.describe(vertex, "vertex")]).write(
                str(d / "classified.ply")
            )
            faces = np.empty(3, dtype=[("vertex_indices", "O")])
            faces["vertex_indices"] = [
                np.array([0, 1, 2]),
                np.array([3, 4, 5]),
                np.array([1, 2, 3]),
            ]
            PlyData(
                [
                    PlyElement.describe(vertex, "vertex"),
                    PlyElement.describe(faces, "face"),
                ]
            ).write(str(d / "mesh.ply"))
            summary = {
                "clusters": [
                    {
                        "cluster_id": i,
                        "centroid": [i * 10 + 1 / 3, 1 / 3, 0],
                        "extent": [1, 1, 0],
                        "point_count": 3,
                        "mean_magnitude": 0.1,
                        "max_magnitude": 0.2,
                    }
                    for i in range(2)
                ]
            }
            (d / "clusters.json").write_text(json.dumps(summary))
            cmd = [
                sys.executable,
                str(ROOT / "pipeline/Scripts/usd_export.py"),
                "--baseline",
                str(d / "classified.ply"),
                "--change",
                str(d / "mesh.ply"),
                "--flagged",
                str(d / "classified.ply"),
                "--clusters",
                str(d / "clusters.json"),
                "--output",
                str(d / "result.usda"),
            ]
            done = subprocess.run(cmd, capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            s = Usd.Stage.Open(str(d / "result.usda"))
            cs = read_clusters(s)
            self.assertEqual(len(cs), 2)
            self.assertTrue(all(not c.shape_paths for c in cs))
            self.assertEqual(s.GetMetadata("metersPerUnit"), 1.0)
            mesh = s.GetPrimAtPath("/World/Compartment/ChangeHighlight")
            self.assertEqual(len(mesh.GetAttribute("faceVertexCounts").Get()), 3)


if __name__ == "__main__":
    unittest.main()
