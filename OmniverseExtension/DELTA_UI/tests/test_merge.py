"""Check the merge keeps the supplied pipeline and older build intact."""
import hashlib
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "exts/delta.robot.ui/delta/robot/ui"))
import pipeline_launcher


class MergeTests(unittest.TestCase):
    def test_callum_pipeline_is_unchanged(self):
        manifest = json.loads((ROOT / "PIPELINE_MANIFEST.json").read_text())
        self.assertEqual(manifest["commit"], "cb4f23ae2c454a457f0846d8c5da0cb85f83d4ae")
        self.assertEqual(len(manifest["sha256"]), 36)
        for path, expected in manifest["sha256"].items():
            with self.subTest(path=path):
                self.assertEqual(hashlib.sha256((ROOT / path).read_bytes()).hexdigest(), expected)

    def test_original_v06_source_was_preserved(self):
        base = ROOT.with_name("06-cluster-blender-pipeline")
        if not base.is_dir():
            self.skipTest("Original local v06 build is not present")
        for path, expected in json.loads((ROOT / "V06_SOURCE_MANIFEST.json").read_text()).items():
            with self.subTest(path=path):
                self.assertEqual(hashlib.sha256((base / path).read_bytes()).hexdigest(), expected)

    def test_builder_launches_original_app_in_separate_environment(self):
        with patch.dict(os.environ, {"PYTHONPATH": "isaac-python", "PYTHONHOME": "isaac-home",
                                     "QT_PLUGIN_PATH": "isaac-qt", "QT_QPA_PLATFORM_PLUGIN_PATH": "isaac-platform"}):
            with patch.object(pipeline_launcher.subprocess, "Popen") as launch:
                pipeline_launcher.launch_pipeline()
        args, kwargs = launch.call_args
        self.assertEqual(args[0], [str(pipeline_launcher.pipeline_python()), "-u",
                                  str(ROOT / "QT-Processing-PipelineV2/Gui/pipeline_applet_qt_template.py")])
        self.assertEqual(kwargs["cwd"], str(ROOT / "QT-Processing-PipelineV2"))
        for name in ("PYTHONPATH", "PYTHONHOME", "QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH"):
            self.assertNotIn(name, kwargs["env"])


if __name__ == "__main__":
    unittest.main()
