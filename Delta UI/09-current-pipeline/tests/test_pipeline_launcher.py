"""Check that processing launches in its own Python environment."""

import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "exts/delta.robot.ui/delta/robot/ui"))
import pipeline_launcher


class PipelineLauncherTests(unittest.TestCase):

    def test_builder_launches_original_app_in_separate_environment(self):
        with patch.dict(
            os.environ,
            {
                "PYTHONPATH": "isaac-python",
                "PYTHONHOME": "isaac-home",
                "QT_PLUGIN_PATH": "isaac-qt",
                "QT_QPA_PLATFORM_PLUGIN_PATH": "isaac-platform",
            },
        ):
            with patch.object(pipeline_launcher.subprocess, "Popen") as launch:
                pipeline_launcher.launch_pipeline()
        args, kwargs = launch.call_args
        self.assertEqual(
            args[0],
            [
                str(pipeline_launcher.pipeline_python()),
                "-u",
                str(ROOT / "pipeline/Gui/main.py"),
            ],
        )
        self.assertEqual(kwargs["cwd"], str(ROOT / "pipeline"))
        for name in (
            "PYTHONPATH",
            "PYTHONHOME",
            "QT_PLUGIN_PATH",
            "QT_QPA_PLATFORM_PLUGIN_PATH",
        ):
            self.assertNotIn(name, kwargs["env"])


if __name__ == "__main__":
    unittest.main()
