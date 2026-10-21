"""Launch the full Qt pipeline separately from Isaac Sim's Python runtime."""

import os
from pathlib import Path
import shutil
import subprocess
import sys

BUILD_ROOT = Path(__file__).resolve().parents[5]
PIPELINE_ROOT = BUILD_ROOT / "pipeline"


def pipeline_python():
    """Locate the separate Python environment used for processing."""
    override = os.environ.get("DELTA_PIPELINE_PYTHON")
    path = (
        Path(override)
        if override
        else BUILD_ROOT
        / ".venv"
        / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    )
    if not path.is_file():
        raise RuntimeError(
            "Run SETUP_PROCESSING.bat in this build before opening the processing pipeline."
        )
    return path


def find_blender():
    """Locate Blender from configuration or standard install folders."""
    for key in ("DELTA_BLENDER", "BLENDER_EXE"):
        candidate = os.environ.get(key)
        if candidate and Path(candidate).is_file():
            return candidate
    found = shutil.which("blender")
    if found:
        return found
    candidates = []
    for key in ("ProgramFiles", "ProgramFiles(x86)"):
        root = Path(os.environ.get(key, "C:/Program Files"))
        candidates.extend(root.glob("Blender Foundation/Blender */blender.exe"))
        steam = root / "Steam/steamapps/common/Blender/blender.exe"
        if steam.is_file():
            candidates.append(steam)
    return str(sorted(candidates)[-1]) if candidates else None


def pipeline_status():
    """Summarise whether processing dependencies are available."""
    try:
        pipeline_python()
        python_status = "Processing environment ready."
    except RuntimeError:
        python_status = "Processing environment needs setup."
    return python_status + (
        " Blender found."
        if find_blender()
        else " Blender not found: install Blender 5.2+ or set DELTA_BLENDER."
    )


def launch_pipeline():
    """Start processing in its own environment and record its output."""
    python = pipeline_python()
    entry = PIPELINE_ROOT / "Gui/main.py"
    if not entry.is_file():
        raise RuntimeError("Bundled processing pipeline is missing.")
    env = os.environ.copy()
    for key in (
        "PYTHONPATH",
        "PYTHONHOME",
        "QT_PLUGIN_PATH",
        "QT_QPA_PLATFORM_PLUGIN_PATH",
    ):
        env.pop(key, None)
    env["PATH"] = str(python.parent) + os.pathsep + env.get("PATH", "")
    log = BUILD_ROOT / "logs/pipeline.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as stream:
        return subprocess.Popen(
            [str(python), "-u", str(entry)],
            cwd=str(PIPELINE_ROOT),
            env=env,
            stdout=stream,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )


if __name__ == "__main__":
    try:
        print(pipeline_status())
        process = launch_pipeline()
        sys.exit(process.wait())
    except Exception as exc:
        print(exc)
        sys.exit(1)
