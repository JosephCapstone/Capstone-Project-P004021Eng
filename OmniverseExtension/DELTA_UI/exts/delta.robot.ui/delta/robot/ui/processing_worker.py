"""Run native point-cloud libraries outside the Isaac Sim process."""
import json
import os
from pathlib import Path
import subprocess
import sys

# Set the processing options for each quality level.
QUALITY_PRESETS = {
    "Low - fastest": {
        "voxel": 0.08,
        "neighbors": 12,
        "std_ratio": 1.50,
        "change_threshold": 0.10,
        "registration_voxel": 0.20,
        "registration_distance": 0.50,
    },
    "Medium - recommended": {
        "voxel": 0.05,
        "neighbors": 20,
        "std_ratio": 1.00,
        "change_threshold": 0.05,
        "registration_voxel": 0.10,
        "registration_distance": 0.25,
    },
    "High - detailed": {
        "voxel": 0.03,
        "neighbors": 30,
        "std_ratio": 0.75,
        "change_threshold": 0.02,
        "registration_voxel": 0.05,
        "registration_distance": 0.10,
    },
}


def build_usd(baseline_path, inspection_path, output_path, quality_name, report):
    worker = Path(__file__).with_name("processing_worker.py")
    # Find the bundled Python interpreter on Windows or Linux.
    kit = Path(sys.executable).resolve().parent
    python = kit / "python" / "python.exe"
    if not python.is_file():
        python = kit / "python.exe"
    if not python.is_file():
        raise RuntimeError("Isaac Sim Python executable was not found")
    # Keep Isaac Sim Python paths out of the worker environment.
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    args = [str(python), "-I", "-u", str(worker), str(baseline_path),
            str(inspection_path) if inspection_path else "", str(output_path), quality_name]
    result = None
    # Run processing in a separate process and collect its messages.
    with subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, encoding="utf-8", errors="replace", env=env,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)) as process:
        for line in process.stdout:
            line = line.rstrip()
            if line.startswith("DELTA_RESULT:"):
                result = json.loads(line[len("DELTA_RESULT:"):])
            else:
                report(line)
        code = process.wait()
    # Report a failed worker instead of crashing Isaac Sim.
    if code or result is None:
        raise RuntimeError(f"Point-cloud worker failed (exit code {code}); see processing log")
    return result


# Load processing libraries only inside the worker process.
if __name__ == "__main__":
    root = Path(__file__).resolve().parents[5]
    deps = Path.home() / "AppData/Local/ov/data/Kit/DELTA/0.5/pip3-envs/default-3.12"
    sys.path.insert(0, str(deps))
    sys.path.insert(0, str(root / "worker-deps"))
    sys.path.insert(0, str(Path(__file__).parent))
    from point_cloud_pipeline import build_usd as process_cloud
    result = process_cloud(sys.argv[1], sys.argv[2] or None, sys.argv[3], sys.argv[4],
                           lambda message: print(message, flush=True))
    print("DELTA_RESULT:" + json.dumps(result), flush=True)
